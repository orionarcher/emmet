import logging
import operator
from datetime import datetime
from itertools import groupby
from typing import Iterable, List, Union

from monty.json import MontyDecoder
from pydantic import BaseModel, Field, validator
from pymatgen.analysis.structure_matcher import ElementComparator, StructureMatcher
from pymatgen.core import Composition, Structure
from pymatgen.entries.computed_entries import ComputedEntry, ComputedStructureEntry

logger = logging.getLogger(__name__)


def generic_groupby(list_in, comp=operator.eq) -> List[int]:
    """
    Group a list of unsortable objects
    Args:
        list_in: A list of generic objects
        comp: (Default value = operator.eq) The comparator
    Returns:
        [int] list of labels for the input list
    """
    list_out = [-1] * len(list_in)
    label_num = 0
    for i1, ls1 in enumerate(list_out):
        if ls1 != -1:
            continue
        list_out[i1] = label_num
        for i2, ls2 in list(enumerate(list_out))[i1 + 1 :]:
            if comp(list_in[i1], list_in[i2]):
                if list_out[i2] is None:
                    list_out[i2] = list_out[i1]
                else:
                    list_out[i1] = list_out[i2]
                    label_num -= 1
        label_num += 1
    return list_out


def s_hash(el):
    return el.data["comp_delith"]


class StructureGroupDoc(BaseModel):
    """
    Group of structure
    """

    material_id: str = Field(
        None,
        description="The combined material_id of the grouped document is given by the numerically smallest task id ",
    )

    structure_matched: bool = Field(
        None,
        description="True if the structure matching was performed to group theses entries together."
        "This is False for groups that contain all the left over entries like the ones that only "
        "contain the ignored species.",
    )

    has_distinct_compositions: bool = Field(
        None, description="True if multiple compositions are present in the group."
    )

    grouped_ids: list = Field(
        None,
        description="A list of materials ids for all of the materials that were grouped together.",
    )

    framework_formula: str = Field(
        None,
        description="The chemical formula for the framework (the materials system without the ignored species).",
    )

    ignored_species: list = Field(None, description="List of ignored atomic species.")

    chemsys: str = Field(
        None,
        description="The chemical system this group belongs to, if the atoms for the ignored species is "
        "present the chemsys will also include the ignored species.",
    )

    last_updated: datetime = Field(
        None,
        description="Timestamp when this document was built.",
    )

    # Make sure that the datetime field is properly formatted
    @validator("last_updated", pre=True)
    def last_updated_dict_ok(cls, v):
        return MontyDecoder().process_decoded(v)

    @classmethod
    def from_grouped_entries(
        cls,
        entries: List[Union[ComputedEntry, ComputedStructureEntry]],
        ignored_species: List[str],
        structure_matched: bool,
    ) -> "StructureGroupDoc":
        """ "
        Assuming a list of entries are already grouped together, create a StructureGroupDoc
        Args:
            entries: A list of entries that is already grouped together.
        """
        all_atoms = set()
        all_comps = set()
        for ient in entries:
            all_atoms |= set(ient.composition.as_dict().keys())
            all_comps.add(ient.composition.reduced_formula)

        common_atoms = all_atoms - set(ignored_species)
        if len(common_atoms) == 0:
            framework_str = "ignored"
        else:
            comp_d = {k: entries[0].composition.as_dict()[k] for k in common_atoms}
            framework_comp = Composition.from_dict(comp_d)
            framework_str = framework_comp.reduced_formula
        ids = [ient.entry_id for ient in entries]
        lowest_id = min(ids, key=_get_id_num)

        fields = {
            "material_id": lowest_id,
            "grouped_ids": ids,
            "structure_matched": structure_matched,
            "framework_formula": framework_str,
            "ignored_species": sorted(ignored_species),
            "chemsys": "-".join(sorted(all_atoms | set(ignored_species))),
            "has_distinct_compositions": len(all_comps) > 1,
        }

        return cls(**fields)

    @classmethod
    def from_ungrouped_structure_entries(
        cls,
        entries: List[Union[ComputedEntry, ComputedStructureEntry]],
        ignored_species: List[str],
        ltol: float = 0.2,
        stol: float = 0.3,
        angle_tol: float = 5.0,
    ) -> List["StructureGroupDoc"]:
        """
        Create a list of StructureGroupDocs from a list of ungrouped entries.

        Args:
            entries: The list of ComputedStructureEntries to process.
            ignored_species: the list of ignored species for the structure matcher
            ltol: length tolerance for the structure matcher
            stol: site position tolerance for the structure matcher
            angle_tol: angel tolerance for the structure matcher
        """

        results = []
        sm = StructureMatcher(
            comparator=ElementComparator(),
            primitive_cell=True,
            ignored_species=ignored_species,
            ltol=ltol,
            stol=stol,
            angle_tol=angle_tol,
        )

        # Add a framework field to each entry's data attribute
        for ient in entries:
            ient.data["framework"] = _get_framework(
                ient.composition.reduced_formula, ignored_species
            )

        # split into groups for each framework, must sort before grouping
        entries.sort(key=lambda x: x.data["framework"])
        framework_groups = groupby(entries, key=lambda x: x.data["framework"])

        cnt_ = 0
        for framework, f_group in framework_groups:
            # if you only have ignored atoms put them into one "ignored" groupd
            f_group_l = list(f_group)
            if framework == "ignored":
                struct_group = cls.from_grouped_entries(
                    f_group_l, ignored_species=ignored_species, structure_matched=False
                )
                cnt_ += len(struct_group.grouped_ids)
                continue

            logger.debug(
                f"Performing structure matching for {framework} with {len(f_group_l)} documents."
            )
            for g in group_entries_with_structure_matcher(f_group_l, sm):
                struct_group = cls.from_grouped_entries(
                    g, ignored_species=ignored_species, structure_matched=True
                )
                cnt_ += len(struct_group.grouped_ids)
                results.append(struct_group)
        if cnt_ != len(entries):
            raise RuntimeError(
                "The number of entries in all groups the end does not match the number of supplied entries documents."
                "Something is seriously wrong, please rebuild the entire database and see if the problem persists."
            )
        return results


def group_entries_with_structure_matcher(
    g, struct_matcher
) -> Iterable[List[Union[ComputedStructureEntry]]]:
    """
    Group the entries together based on similarity of the  primitive cells
    Args:
        g: a list of entries
    Returns:
        subgroups: subgroups that are grouped together based on structure similarity
    """
    labs = generic_groupby(
        g,
        comp=lambda x, y: struct_matcher.fit(x.structure, y.structure, symmetric=True),
    )
    for ilab in set(labs):
        sub_g = [g[itr] for itr, jlab in enumerate(labs) if jlab == ilab]
        yield [el for el in sub_g]


def _get_id_num(task_id) -> Union[int, str]:
    if isinstance(task_id, int):
        return task_id
    if isinstance(task_id, str):
        return int(task_id.split("-")[-1])
    else:
        raise ValueError("TaskID needs to be either a number or of the form xxx-#####")


def _get_framework(formula, ignored_species) -> str:
    """
    Return the reduced formula of the entry without any of the ignored species
    Return 'ignored' if the all the atoms are ignored
    """
    dd_ = Composition(formula).as_dict()
    if dd_.keys() == set(ignored_species):
        return "ignored"
    for ignored_sp in ignored_species:
        if ignored_sp in dd_:
            dd_.pop(ignored_sp)
    return Composition.from_dict(dd_).reduced_formula
