# -*- coding: utf-8 -*-
"""Compute features on the BUs and then aggregate them."""
from abc import abstractmethod
from typing import Collection, List, Optional, Tuple, Union

import numpy as np
from matminer.featurizers.base import MultipleFeaturizer
from pydantic import BaseModel
from pymatgen.analysis.graphs import MoleculeGraph, StructureGraph
from pymatgen.core import IMolecule, IStructure, Molecule, Structure

from mofdscribe.featurizers.base import MOFBaseFeaturizer, MOFMultipleFeaturizer
from mofdscribe.featurizers.bu.utils import boxed_molecule
from mofdscribe.featurizers.utils import nan_array, set_operates_on
from mofdscribe.featurizers.utils.aggregators import ARRAY_AGGREGATORS
from mofdscribe.mof import MOF

if False:
    from moffragmentor import SBU
__all__ = ("BUFeaturizer", "BranchingSitesFeaturizer", "BindingSitesFeaturizer")


# since fragmentation is not super straightforward,
# we give the option to provide the fragments directly
class MOFBBs(BaseModel):
    """Container for MOF building blocks."""

    nodes: Optional[List[Union[Structure, Molecule, IStructure, IMolecule]]]
    linkers: Optional[List[Union[Structure, Molecule, IStructure, IMolecule]]]


def _structuregraph_from_indices(mof, indices) -> StructureGraph:
    from moffragmentor.utils import remove_all_nodes_not_in_indices

    structure_graph = mof.structure_graph.__copy__()
    remove_all_nodes_not_in_indices(structure_graph, indices)
    return structure_graph


def structuregraph_from_bu(mof: "MOF", bu: "SBU") -> StructureGraph:
    return _structuregraph_from_indices(mof, bu._original_indices)


class BUFeaturizer(MOFBaseFeaturizer):
    """
    Compute features on the BUs and then aggregate them.

    This can be useful if you want to compute some features on the BUs
    and them aggregrate them to obtain one fixed-length feature vector for the MOF.

    .. warning::
        Note that, currently, not all featurizers can operate on both
        Structures and Molecules.
        If you want to include featurizers that can only operate on one type
        (e.g. :py:obj:`~mofdscribe.bu.rdkitadaptor.RDKitAdaptor`
        and :py:obj:`~mofdscribe.chemistry.amd.AMD`)
        then you need to create two separate MOFBBs and BUFeaturizer objects.

    Examples:
        >>> from mofdscribe.bu import BUFeaturizer, MOFBBs
        >>> from mofdscribe.bu.rdkitadaptor import RDKitAdaptor
        >>> from rdkit.Chem.Descriptors3D import Asphericity
        >>> from pymatgen.core import Molecule
        >> from pymatgen.io.babel import BabelMolAdaptor
        >>> base_featurizer = RDKitAdaptor(featurizer=Asphericity, feature_labels=["asphericity"])
        >>> bu_featurizer = BUFeaturizer(featurizer=base_featurizer, aggregations=("mean", "std", "min", "max"))
        >>> bu_featurizer.featurize(
                mofbbs=MOFBBs(nodes=[BabelMolAdaptor.from_string(
                    "[CH-]1C=CC=C1.[CH-]1C=CC=C1.[Fe+2]", "smi").pymatgen_mol],
                linkers=[BabelMolAdaptor.from_string("CCCC", "smi").pymatgen_mol]))
    """

    _NAME = "BUFeaturizer"

    def __init__(
        self,
        featurizer: MOFBaseFeaturizer,
        aggregations: Tuple[str] = ("mean", "std", "min", "max"),
    ) -> None:
        """
        Construct a new BUFeaturizer.

        Args:
            featurizer (MOFBaseFeaturizer): The featurizer to use.
                Currently, we do not support `MultipleFeaturizer`s.
                Please, instead, use multiple BUFeaturizers.
                If you use a featurizer that is not implemented in mofdscribe
                (e.g. a matminer featurizer), you need to wrap using a method
                that describes on which data objects the featurizer can operate on.
                If you do not do this, we default to assuming that it operates on structures.
            aggregations (Tuple[str]): The aggregations to use.
                Must be one of :py:obj:`ARRAY_AGGREGATORS`.

        Raises:
            ValueError: If the featurizer is a `MultipleFeaturizer`.
        """
        if isinstance(featurizer, (MOFMultipleFeaturizer, MultipleFeaturizer)):
            raise ValueError("BUFeaturizer does not support MultipleFeaturizer.")
        self._featurizer = featurizer
        self._aggregations = aggregations
        set_operates_on(self, featurizer)

    def feature_labels(self) -> List[str]:
        labels = []
        base_labels = self._featurizer.feature_labels()
        for bb in ["node", "linker"]:
            for aggregation in self._aggregations:
                for label in base_labels:
                    labels.append(f"{self._NAME}_{bb}_{aggregation}_{label}")
        return labels

    def _extract_bbs(
        self,
        mof: Optional[MOF],
        mofbbs: Optional[MOFBBs] = None,
    ):
        if mof is None and mofbbs is None:
            raise ValueError("You must provide a structure or mofbbs.")

        if mof is not None:
            fragments = mof.fragments
            if self._operates_on & set([Molecule]):
                linkers = [linker.molecule for linker in fragments.linkers]
                nodes = [node.molecule for node in fragments.nodes]
            elif self._operates_on & set([StructureGraph]):
                linkers = [structuregraph_from_bu(mof, linker) for linker in fragments.linkers]
                nodes = [structuregraph_from_bu(mof, node) for node in fragments.nodes]
            elif self._operates_on & set([MoleculeGraph]):
                linkers = [linker.molecule_graph for linker in fragments.linkers]
                nodes = [node.molecule_graph for node in fragments.nodes]
            else:
                # create a boxed structure
                linkers = [linker._get_boxed_structure() for linker in fragments.linkers]
                nodes = [node._get_boxed_structure() for node in fragments.nodes]

        if mofbbs is not None:
            linkers = list(mofbbs.linkers) if mofbbs.linkers is not None else []
            nodes = list(mofbbs.nodes) if mofbbs.nodes is not None else []
            types = [type(node) for node in nodes] + [type(linker) for linker in linkers]

            if not len(set(types)) == 1:
                raise ValueError("All nodes and linkers must be of the same type.")

            this_type = types[0]
            if this_type in (Structure, IStructure) and self._operates_on & set(
                [Molecule, Structure]
            ):
                # this is the simple case, we do not need to convert to molecules
                pass
            elif this_type in (Molecule, IMolecule) and self._operates_on & set(
                [Molecule, Structure]
            ):
                # again simple case, we do not need to convert to structures

                pass
            elif this_type in (Molecule, IMolecule) and (self._operates_on & set([Molecule])):
                # we need to convert to structures
                nodes = [boxed_molecule(node) for node in nodes]
                linkers = [boxed_molecule(linker) for linker in linkers]
            elif this_type in (Structure, IStructure) and (self._operates_on & set([Molecule])):
                raise ValueError(
                    "You provided structures for a featurizer that operates on molecules. "
                    / "Cannot automatically convert to molecules from structures."
                )
            else:
                raise RuntimeError("Unexpected type of nodes or linkers.")

        return nodes, linkers

    def _fit(self):
        raise NotImplementedError("BUFeaturizer does not support _fit.")

    def fit(
        self,
        mofs: Optional[Collection[MOF]] = None,
        mofbbs: Optional[Collection[MOFBBs]] = None,
    ) -> None:
        """
        Fit the featurizer to the given structures.

        Args:
            mofs (Collection[MOF], optional): The MOFs to featurize.
            mofbbs (Collection[MOFBBs], optional): The MOF fragments (nodes and linkers).
        """
        all_nodes, all_linkers = [], []
        if mofs is not None:
            for mof in mofs:
                nodes, linkers = self._extract_bbs(mof=mof)
                all_nodes.extend(nodes)
                all_linkers.extend(linkers)
        if mofbbs is not None:
            for mofbb in mofbbs:
                nodes, linkers = self._extract_bbs(mofbbs=mofbb)
                all_nodes.extend(nodes)
                all_linkers.extend(linkers)

        all_fragments = all_nodes + all_linkers
        self._featurizer._fit(all_fragments)

    def _featurize(self):
        raise NotImplementedError("BUFeaturizer does not support _featurize.")

    def featurize(
        self,
        mof: Optional[MOF] = None,
        mofbbs: Optional[MOFBBs] = None,
    ) -> np.ndarray:
        """
        Compute features on the BUs and then aggregate them.

        If you provide a structure, we will fragment the MOF into BUs.
        If you already have precomputed fragements or only want to consider a subset
        of the BUs, you can provide them manually via the `mofbbs` argument.

        If you manually provide the `mofbbs`,  we will convert molecules to structures
        where possible.

        Args:
            mof (MOF, optional): The structure to featurize.
            mofbbs (MOFBBs, optional): The MOF fragments (nodes and linkers).

        Returns:
            A numpy array of features.
        """
        # if i know what the featurizer wants, I can always cast to a structure
        num_features = len(self._featurizer.feature_labels())
        nodes, linkers = self._extract_bbs(mof, mofbbs)
        linker_feats = [self._featurizer._featurize(linker) for linker in linkers]
        if not linker_feats:
            linker_feats = [nan_array(num_features)]

        node_feats = [self._featurizer._featurize(node) for node in nodes]
        if not node_feats:
            node_feats = [nan_array(num_features)]

        aggregated_linker_feats = []
        for aggregation in self._aggregations:
            aggregated_linker_feats.extend(ARRAY_AGGREGATORS[aggregation](linker_feats, axis=0))
        aggregated_linker_feats = np.array(aggregated_linker_feats)

        aggregated_node_feats = []
        for aggregation in self._aggregations:
            aggregated_node_feats.extend(ARRAY_AGGREGATORS[aggregation](node_feats, axis=0))
        aggregated_node_feats = np.array(aggregated_node_feats)

        return np.concatenate((aggregated_node_feats, aggregated_linker_feats))

    def citations(self) -> List[str]:
        return self._featurizer.citations()

    def implementors(self) -> List[str]:
        return self._featurizer.implementors()


class _BUSubBaseFeaturizer(BUFeaturizer):
    def featurize(
        self,
        mof: Optional[MOF] = None,
    ) -> np.ndarray:
        """
        Compute features on the BUs and then aggregate them.

        If you provide a structure, we will fragment the MOF into BUs.
        If you already have precomputed fragements or only want to consider a subset
        of the BUs, you can provide them manually via the `mofbbs` argument.

        If you manually provide the `mofbbs`,  we will convert molecules to structures
        where possible.

        Args:
            mof (MOF, optional): The structure to featurize.

        Returns:
            A numpy array of features.
        """
        # if i know what the featurizer wants, I can always cast to a structure
        num_features = len(self._featurizer.feature_labels())
        nodes, linkers = self._extract(mof)
        linker_feats = [self._featurizer._featurize(linker) for linker in linkers]
        if not linker_feats:
            linker_feats = [nan_array(num_features)]

        node_feats = [self._featurizer._featurize(node) for node in nodes]
        if not node_feats:
            node_feats = [nan_array(num_features)]

        aggregated_linker_feats = []
        for aggregation in self._aggregations:
            aggregated_linker_feats.extend(ARRAY_AGGREGATORS[aggregation](linker_feats, axis=0))
        aggregated_linker_feats = np.array(aggregated_linker_feats)

        aggregated_node_feats = []
        for aggregation in self._aggregations:
            aggregated_node_feats.extend(ARRAY_AGGREGATORS[aggregation](node_feats, axis=0))
        aggregated_node_feats = np.array(aggregated_node_feats)

        return np.concatenate((aggregated_node_feats, aggregated_linker_feats))

    @abstractmethod
    def _extract(self, mof: MOF) -> Tuple[List[Structure], List[Structure]]:
        raise NotImplementedError()

    def fit(self, mofs: Collection[MOF]):
        all_nodes, all_linkers = [], []
        for mof in mofs:
            nodes, linkers = self._extract(mof)
            all_nodes.extend(nodes)
            all_linkers.extend(linkers)
        self._featurizer.fit(all_nodes + all_linkers)

    def citations(self) -> List[str]:
        return self._featurizer.citations()

    def implementors(self) -> List[str]:
        return self._featurizer.implementors()


# ToDo: generalize extract depending on what the featurizer operates on
class BindingSitesFeaturizer(_BUSubBaseFeaturizer):
    """A special BU featurizer that operates on structures spanned by "binding sites".

    We define binding sites as the linker atoms that directly connect to
    the metal atoms of a node.
    A good example are the carboxy oxygen atoms in a copper paddlewheel.
    From more details see the `moffragmentor documentation
    <https://moffragmentor.readthedocs.io/en/latest/background.html#fragmentation>`_.


    Example:
        >>> from mofdscribe.mof import MOF
        >>> from mofdscribe.featurizers.bu.bu_featurizer import BindingSitesFeaturizer
        >>> from mofdscribe.featurizers.bu.lsop_featurizer import LSOP
        >>> import pandas as pd
        >>> mof = MOF.from_file("mof_file.cif")
        >>> featurizer = BindingSitesFeaturizer(LSOP())
        >>> feats = featurizer.featurize(mof)
        >>> pd.DataFrame([feats], columns=featurizer.feature_labels())
    """

    _NAME = "BindingSitesFeaturizer"

    def _extract(self, mof: MOF):
        if Structure in self._operates_on:
            linkers = [linker._get_binding_sites_structure() for linker in mof.fragments.linkers]
            nodes = [node._get_binding_sites_structure() for node in mof.fragments.nodes]
            return nodes, linkers
        elif StructureGraph in self._operates_on:
            linkers = [
                _structuregraph_from_indices(mof, linker._original_binding_indices)
                for linker in mof.fragments.linkers
            ]
            nodes = [
                _structuregraph_from_indices(mof, node._original_binding_indices)
                for node in mof.fragments.nodes
            ]
        elif Molecule in self._operates_on:
            raise NotImplementedError("BindingSitesFeaturizer does not support Molecule yet.")
        else:
            raise NotImplementedError(
                f"BindingSitesFeaturizer does not support featurizers operating on {self._operates_on}."
            )


class BranchingSitesFeaturizer(_BUSubBaseFeaturizer):
    """A special BU featurizer that operates on structures spanned by "branching sites".

    Branching sites are defined as the sites where a linker connects to a node.
    The most common example is the carbon atom in a carboxy group.
    From more details see the `moffragmentor documentation
    <https://moffragmentor.readthedocs.io/en/latest/background.html#fragmentation>`_.


    Example:
        >>> from mofdscribe.mof import MOF
        >>> from mofdscribe.featurizers.bu.bu_featurizer import BranchingSitesFeaturizer
        >>> from mofdscribe.featurizers.bu.lsop_featurizer import LSOP
        >>> import pandas as pd
        >>> mof = MOF.from_file("mof_file.cif")
        >>> featurizer = BranchingSitesFeaturizer(LSOP())
        >>> feats = featurizer.featurize(mof)
        >>> pd.DataFrame([feats], columns=featurizer.feature_labels())
    """

    _NAME = "BranchingSitesFeaturizer"

    def _extract(self, mof: MOF):
        if Structure in self._operates_on:
            linkers = [linker._get_branching_sites_structure() for linker in mof.fragments.linkers]
            nodes = [node._get_branching_sites_structure() for node in mof.fragments.nodes]
            return nodes, linkers
        elif StructureGraph in self._operates_on:
            linkers = [
                _structuregraph_from_indices(mof, linker._original_binding_indices)
                for linker in mof.fragments.linkers
            ]
            nodes = [
                _structuregraph_from_indices(mof, node._original_binding_indices)
                for node in mof.fragments.nodes
            ]
        elif Molecule in self._operates_on:
            raise NotImplementedError("BranchingSitesFeaturizer does not support Molecule yet.")
        else:
            raise NotImplementedError(
                f"BranchingSitesFeaturizer does not support featurizers operating on {self._operates_on}."
            )
