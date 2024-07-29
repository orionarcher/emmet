import warnings
from typing import Optional, Union

import numpy as np

from openff.interchange import Interchange
import openff.toolkit as tk
from emmet.core.classical_md import MoleculeSpec
from MDAnalysis import Universe
from solvation_analysis.solute import Solute

from pathlib import Path


def create_universe(
    interchange: Interchange,
    mol_specs: Optional[list[MoleculeSpec]],
    traj_file: Union[Path, str],
    traj_format=None,
):
    """
    Create a Universe object from an Interchange object and a trajectory file.

    Parameters
    ----------
    interchange : Interchange
        The Interchange object containing the topology and parameters.
    mol_specs : list[MoleculeSpec] or None
        A list of MoleculeSpec objects or None.
    traj_file : Path or str
        The path to the trajectory file.
    traj_format : str, optional
        The format of the trajectory file.

    Returns
    -------
    Universe
        The created Universe object.
    """
    # TODO: profile this
    topology = interchange.to_openmm_topology()

    u = Universe(
        topology,
        str(traj_file),
        format=traj_format,
    )

    # TODO: this won't work
    mols = [mol for mol in interchange.topology.molecules]

    label_types(u, mols)
    label_resnames(u, mols, mol_specs)
    label_charges(u, mols, mol_specs)

    return u


def label_types(u: Universe, mols: list[tk.Molecule]):
    """
    Label atoms in the Universe with unique types based on the molecules.

    Parameters
    ----------
    u : Universe
        The Universe object to label.
    mols : list[tk.Molecule]
        The list of Molecule objects.
    """
    # add unique counts for each
    offset = 0
    mol_types = {}
    for mol in set(mols):
        mol_types[mol] = range(offset, offset + mol.n_atoms)
        offset += mol.n_atoms
    all_types = np.concatenate([mol_types[mol] for mol in mols])
    u.add_TopologyAttr("types", all_types)


def label_resnames(
    u: Universe, mols: list[tk.Molecule], mol_specs: Optional[list[MoleculeSpec]]
):
    """
    Label atoms in the Universe with residue names.

    Parameters
    ----------
    u : Universe
        The Universe object to label.
    mols : list[tk.Molecule]
        The list of Molecule objects.
    mol_specs : list[MoleculeSpec] or None
        A list of MoleculeSpec objects or None.
    """
    if mol_specs:
        resname_list = [[spec.name] * spec.count for spec in mol_specs]
        resnames = np.concatenate(resname_list)
    else:
        resname_list = [mol.to_smiles() for mol in mols]
        resnames = np.array(resname_list)
    u.add_TopologyAttr("resnames", resnames)


def label_charges(
    u: Universe, mols: list[tk.Molecule], mol_specs: Optional[list[MoleculeSpec]]
):
    """
    Label atoms in the Universe with partial charges.

    Parameters
    ----------
    u : Universe
        The Universe object to label.
    mols : list[tk.Molecule]
        The list of Molecule objects.
    mol_specs : list[MoleculeSpec]
        A list of MoleculeSpec objects.
    """
    charge_arrays = []
    if mol_specs:
        for spec in mol_specs:
            mol = tk.Molecule.from_json(spec.openff_mol)
            charge_arr = np.tile(mol.partial_charges / spec.charge_scaling, spec.count)
            charge_arrays.append(charge_arr)
    else:
        warnings.warn(
            "`mol_specs` are not present so charges cannot be unscaled. "
            "If charges were scaled, conductivity calculations will be inaccurate."
        )
        for mol in mols:
            charge_arrays.append(mol.partial_charges)
    charges = np.concatenate(charge_arrays).magnitude
    u.add_TopologyAttr("charges", charges)


def create_solute(
    u: Universe,
    solute_name: str,
    networking_solvents: Optional[list[str]] = None,
    fallback_radius: Optional[float] = None,
    include_solute_in_solvents=False,
    analysis_classes=["coordination", "pairing", "speciation", "networking"],
    step=1,
):
    """
    Create a Solute object from a Universe object.

    Parameters
    ----------
    u : Universe
        The Universe object containing the solute and solvent atoms.
    solute_name : str
        The residue name of the solute.
    networking_solvents : list[str] or None, optional
        A list of residue names of networking solvents or None.
    fallback_radius : float or None, optional
        The fallback radius for kernel calculations or None.
    include_solute_in_solvents : bool, optional
        Whether to include the solute in the solvents dictionary. Default is False.
    analysis_classes : list[str], optional
        The analysis classes to run. Default is ("coordination", "pairing", "speciation", "networking").
    step : int, optional
        The step size for the analysis. Default is 1.

    Returns
    -------
    Solute
        The created Solute object.
    """
    solute = u.select_atoms(f"resname {solute_name}")

    unique_resnames = np.unique(u.atoms.residues.resnames)
    solvents = {
        resname: u.select_atoms(f"resname {resname}") for resname in unique_resnames
    }
    if not include_solute_in_solvents:
        solvents.pop(solute_name, None)

    solute = Solute.from_atoms(
        solute,
        solvents,
        solute_name=solute_name,
        analysis_classes=analysis_classes,
        networking_solvents=networking_solvents,
        kernel_kwargs={"default": fallback_radius},
    )
    solute.run(step=step)
    return solute


def identify_solute(u: Universe):
    """
    Identify the solute in a Universe object.

    Currently just finds the name of a sinlge cation based on the
    partial charges in the universe.

    Parameters
    ----------
    u : Universe
        The Universe object

    Returns
    -------
    str
        The residue name of the solute.
    """
    cation_residues = u.residues[u.residues.charges > 0.01]
    unique_names = np.unique(cation_residues.resnames)
    if len(unique_names) > 1:
        # TODO: fail gracefully?
        raise ValueError("Multiple cationic species detected, not yet supported.")
    return unique_names[0]


def identify_networking_solvents(u: Universe):
    """
    Identify the networking solvents in a Universe object.

    Currently just finds the name of all anions based on the
    partial charges in the universe.

    Parameters
    ----------
    u : Universe
        The Universe object

    Returns
    -------
    list[str]
        The residue names of the networking solvents.
    """
    # currently just anions
    anion_residues = u.residues[u.residues.charges < -0.01]
    unique_names = np.unique(anion_residues.resnames)
    return list(unique_names)
