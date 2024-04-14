import warnings

import numpy as np

from openff.interchange import Interchange
import openff.toolkit as tk
from emmet.core.classical_md import MoleculeSpec
from MDAnalysis import Universe
from solvation_analysis.solute import Solute
from solvation_analysis.rdf_parser import identify_cutoff_scipy

from pathlib import Path


def create_universe(
    interchange: Interchange,
    mol_specs: list[MoleculeSpec] | None,
    traj_file: Path | str,
    traj_format=None,
):
    topology = interchange.to_openmm_topology()

    u = Universe(topology, str(traj_file), format=traj_format)

    mols = [mol for mol in interchange.topology.molecules]

    label_types(u, mols)

    label_resnames(u, mols, mol_specs)

    label_charges(u, mols, mol_specs)

    return u


def label_types(u: Universe, mols: list[tk.Molecule]):
    # add unique counts for each
    offset = 0
    mol_types = {}
    for mol in set(mols):
        mol_types[mol] = range(offset, offset + mol.n_atoms)
        offset += mol.n_atoms
    all_types = np.concatenate([mol_types[mol] for mol in mols])
    u.add_TopologyAttr("types", all_types)


def label_resnames(
    u: Universe, mols: list[tk.Molecule], mol_specs: list[MoleculeSpec] | None
):
    if mol_specs:
        resname_list = [[spec.name] * spec.count for spec in mol_specs]
        resnames = np.concatenate(resname_list)
    else:
        resname_list = [mol.to_smiles() for mol in mols]
        resnames = np.array(resname_list)
    u.add_TopologyAttr("resnames", resnames)


def label_charges(u: Universe, mols: list[tk.Molecule], mol_specs: list[MoleculeSpec]):
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


def find_peaks(bins, rdf, fallback_radius: float = 3):
    peak = identify_cutoff_scipy(bins, rdf, failure_behavior="warn")
    return peak if not np.isnan(peak) else fallback_radius


def mol_specs_from_interchange(interchange: Interchange) -> list[MoleculeSpec]:
    return


def create_solute(
    u: Universe,
    solute_name: str,
    networking_solvents: list[str] | None = None,
    fallback_radius: float | None = None,
):
    solute = u.select_atoms(f"resname {solute_name}")

    unique_resnames = np.unique(u.atoms.residues.resnames)
    solvents = {
        resname: u.select_atoms(f"resname {resname}")
        for resname in unique_resnames
        # if resname != solute_name
    }

    solute = Solute.from_atoms(
        solute,
        solvents,
        solute_name=solute_name,
        analysis_classes="all",
        rdf_kernel=find_peaks,
        networking_solvents=networking_solvents,
        kernel_kwargs={"fallback_radius": fallback_radius},
    )
    solute.run()
    return solute


def identify_solute(u: Universe):
    # currently just cations
    cation_residues = u.residues[u.residues.charges > 0.01]
    unique_names = np.unique(cation_residues.resnames)
    if len(unique_names) > 1:
        # TODO: fail gracefully?
        raise ValueError("Multiple cationic species detected, not yet supported.")
    return unique_names[0]


def identify_networking_solvents(u: Universe):
    # currently just anions
    anion_residues = u.residues[u.residues.charges > 0.01]
    unique_names = np.unique(anion_residues.resnames)
    return list(unique_names)


# def molgraph_from_molecules(molecules: Iterable[tk.Molecule]):
#     """
#     This is designed to closely mirror the graph structure generated by tk.Molecule.to_networkx
#
#     TODO: move to pymatgen.
#     """
#     molgraph = MoleculeGraph.with_empty_graph(
#         Molecule([], []),
#         name="none",
#     )
#     p_table = {el.Z: str(el) for el in Element}
#     total_charge = 0
#     cum_atoms = 0
#     for molecule in molecules:
#         if molecule.conformers is not None:
#             coords = molecule.conformers[0].magnitude
#         else:
#             coords = np.zeros((molecule.n_atoms, 3))
#         for j, atom in enumerate(molecule.atoms):
#             molgraph.insert_node(
#                 cum_atoms + j,
#                 p_table[atom.atomic_number],
#                 coords[j, :],
#             )
#             molgraph.graph.nodes[cum_atoms + j]["atomic_number"] = atom.atomic_number
#             molgraph.graph.nodes[cum_atoms + j]["is_aromatic"] = atom.is_aromatic
#             molgraph.graph.nodes[cum_atoms + j][
#                 "stereochemistry"
#             ] = atom.stereochemistry
#             # set partial charge as a pure float
#             partial_charge = (
#                 None if atom.partial_charge is None else atom.partial_charge.magnitude
#             )
#             molgraph.graph.nodes[cum_atoms + j]["partial_charge"] = partial_charge
#             # set formal charge as a pure float
#             formal_charge = atom.formal_charge.magnitude  # type: ignore
#             molgraph.graph.nodes[cum_atoms + j]["formal_charge"] = formal_charge
#             total_charge += formal_charge
#         for bond in molecule.bonds:
#             molgraph.graph.add_edge(
#                 cum_atoms + bond.atom1_index,
#                 cum_atoms + bond.atom2_index,
#                 bond_order=bond.bond_order,
#                 is_aromatic=bond.is_aromatic,
#                 stereochemistry=bond.stereochemistry,
#             )
#         # formal_charge += molecule.total_charge
#         cum_atoms += molecule.n_atoms
#     molgraph.molecule.set_charge_and_spin(charge=total_charge)
#     return molgraph
#
#


#
# def get_set_contents(
#     mol_specs: List[Dict[str, Union[str, int, tk.Molecule]]],
# ) -> SetContents:
#     openff_counts = {spec["openff_mol"]: spec["count"] for spec in mol_specs}
#
#     # replace openff mols with molgraphs in mol_specs
#     molgraph_specs = []
#     for spec in mol_specs:
#         spec = copy.deepcopy(spec)
#         openff_mol = spec.pop("openff_mol")
#         spec["molgraph"] = molgraph_from_openff_mol(openff_mol)
#         mol_spec = MoleculeSpec(**spec)
#         molgraph_specs.append(mol_spec)
#
#     # calculate atom types for analysis convenience
#     atom_types = smiles_to_atom_types(openff_counts)  # type: ignore
#     atom_resnames = smiles_to_resnames(mol_specs)
#
#     # calculate force field and charge method
#     force_fields = list({spec["force_field"] for spec in mol_specs})
#     charge_methods = list({spec["charge_method"] for spec in mol_specs})
#
#     return SetContents(
#         molecule_specs=molgraph_specs,
#         force_fields=force_fields,
#         partial_charge_methods=charge_methods,
#         atom_types=atom_types,
#         atom_resnames=atom_resnames,
#     )
