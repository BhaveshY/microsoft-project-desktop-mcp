from __future__ import annotations

import builtins
import copy
import importlib
import os
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, time
from unittest.mock import patch

from ms_project_mcp.errors import BackendExecutionError, DispatchState, ErrorCode, MspError
from ms_project_mcp.factory import create_backend
from ms_project_mcp.ledger import InMemoryOperationLedger
from ms_project_mcp.live import LiveProjectBackend
from ms_project_mcp.models import (
    AddDependency,
    AnalysisKind,
    CreateAssignment,
    CreateCalendar,
    CreateResource,
    CreateTask,
    CloseDisposition,
    DeleteCalendar,
    DeleteTask,
    DesktopProjectDetection,
    ExportOptions,
    MoveTask,
    ObjectKind,
    ObjectRef,
    Ownership,
    ProjectAction,
    ProjectRequest,
    ProjectState,
    QueryEntity,
    ScheduleCommand,
    ScheduleOptions,
    SetBaseline,
    CalendarException,
    ClearBaseline,
    TaskProgressUpdate,
    TaskConstraintType,
    TaskType,
    TimephasedWorkUpdate,
    UpdateCalendar,
    UpdateProjectProperties,
    UpdateResource,
    UpdateTask,
    VerificationLevel,
    Weekday,
    WorkingDay,
    WorkingInterval,
)
from ms_project_mcp.sta import StaHost, StaHostState
from ms_project_mcp.service import ProjectService
from ms_project_mcp.unavailable import UnavailableProjectBackend


class _ThreadChecked:
    def __init__(self, factory) -> None:
        object.__setattr__(self, "_factory", factory)
        object.__setattr__(self, "_closed", False)

    def __getattribute__(self, name):
        if name.startswith("_"):
            return object.__getattribute__(self, name)
        factory = object.__getattribute__(self, "_factory")
        factory.record_access()
        if object.__getattribute__(self, "_closed") and name not in {"Close"}:
            raise RuntimeError("fake Project object is closed")
        return object.__getattribute__(self, name)

    def __setattr__(self, name, value):
        if not name.startswith("_") and hasattr(self, "_factory"):
            object.__getattribute__(self, "_factory").record_access()
        object.__setattr__(self, name, value)


class _FakeTask(_ThreadChecked):
    def __init__(self, factory, unique_id: int, name: str, parent=None) -> None:
        super().__init__(factory)
        self.UniqueID = unique_id
        self.ID = unique_id
        self.Name = name
        self.Duration = 480
        self.Milestone = False
        self.OutlineParent = parent
        self.OutlineLevel = 1 if parent is None else parent.OutlineLevel + 1
        self.PercentComplete = 0
        self.ActualDuration = 0
        self.RemainingDuration = 480
        self.ActualWork = 0
        self.RemainingWork = 480
        self.ActualStart = None
        self.ActualFinish = None
        self.FixedCost = 0
        self.FixedCostAccrual = 3
        self.Start = datetime(2027, 1, 1, 8, 0)
        self.Finish = datetime(2027, 1, 1, 17, 0)
        self.Critical = unique_id == 22
        self.TotalSlack = 0
        self.ConstraintType = 0
        self.ConstraintDate = None
        self.Deadline = None
        self.Type = 0
        self.EffortDriven = False
        self.Manual = False
        self.Priority = 500
        self.Notes = ""
        self.Calendar = "None"
        self.CalendarObject = None
        self.IgnoreResourceCalendar = False
        self.OutlineNumber = ""
        self.Cost = 100
        self.BaselineCost = 80
        self.CostVariance = 20
        self.FinishVariance = 0
        self.BCWS = 80
        self.BCWP = 75
        self.ACWP = 70
        self.SV = -5
        self.BaselineStart = datetime(2027, 1, 1, 8, 0)
        self.TaskDependencies = []

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name == "Calendar" and hasattr(self, "_project"):
            calendar = next(
                (item for item in self._project.BaseCalendars if item.Name == value),
                None,
            )
            super().__setattr__("CalendarObject", calendar)

    def Delete(self) -> None:
        descendants = [
            item
            for item in self._project.Tasks._items
            if item is self or self._is_descendant(item)
        ]
        for item in reversed(descendants):
            self._project.Tasks.remove(item)

    @property
    def Summary(self) -> bool:
        return any(self._is_descendant(item) for item in self._project.Tasks._items)

    def _is_descendant(self, candidate) -> bool:
        parent = candidate.OutlineParent
        while parent is not None:
            if parent is self:
                return True
            parent = parent.OutlineParent
        return False

    def OutlineIndent(self) -> None:
        items = self._project.Tasks._items
        index = items.index(self)
        target_parent_level = self.OutlineLevel
        parent = next(
            (item for item in reversed(items[:index]) if item.OutlineLevel == target_parent_level),
            None,
        )
        if parent is None:
            return
        self.OutlineParent = parent
        self.OutlineLevel += 1
        self._factory.outline_indent_calls.append(self.UniqueID)
        self._project.Saved = False

    def OutlineOutdent(self) -> None:
        if self.OutlineLevel <= 1:
            return
        self.OutlineParent = (
            self.OutlineParent.OutlineParent if self.OutlineParent is not None else None
        )
        self.OutlineLevel -= 1
        self._factory.outline_outdent_calls.append(self.UniqueID)
        self._project.Saved = False


class _FakeTasks(_ThreadChecked):
    def __init__(self, factory, project) -> None:
        super().__init__(factory)
        self._project = project
        self._items = []

    def Add(self, name: str, Before=None):
        uid = max((item.UniqueID for item in self._items), default=0) + 1
        task = _FakeTask(self._factory, uid, name)
        object.__setattr__(task, "_project", self._project)
        task.TaskDependencies = _FakeTaskDependencies(self._factory, self._project, task)
        if hasattr(self._project, "Assignments"):
            task.Assignments = self._project.Assignments
        if Before is None:
            self._items.append(task)
        else:
            index = next(index for index, item in enumerate(self._items) if item.ID == Before)
            insertion_row = self._items[index]
            task.OutlineLevel = insertion_row.OutlineLevel
            task.OutlineParent = insertion_row.OutlineParent
            self._items.insert(index, task)
        self._reindex()
        self._factory.last_created_task = task
        self._factory.task_add_calls.append((name, Before))
        self._project.Saved = False
        return task

    def remove(self, item):
        self._items.remove(item)
        self._reindex()
        self._project.Saved = False

    def _reindex(self):
        for index, item in enumerate(self._items, start=1):
            item.ID = index

    def __iter__(self):
        return iter(self._items)


class _FakeDependency(_ThreadChecked):
    def __init__(self, factory, predecessor, successor) -> None:
        super().__init__(factory)
        self.From = predecessor
        self.To = successor
        self.Type = 1
        self.Lag = 60

    def Delete(self) -> None:
        self._project.Dependencies.remove(self)
        self.To.TaskDependencies.remove(self)
        self._project.Saved = False


class _FakeTaskDependencies(_ThreadChecked):
    def __init__(self, factory, project, successor) -> None:
        super().__init__(factory)
        self._project = project
        self._successor = successor
        self._items = []

    def Add(self, predecessor, dependency_type, lag):
        dependency = _FakeDependency(self._factory, predecessor, self._successor)
        dependency.Type = dependency_type
        dependency.Lag = lag
        object.__setattr__(dependency, "_project", self._project)
        self._items.append(dependency)
        self._project.Dependencies.append(dependency)
        self._project.Saved = False
        return dependency

    def remove(self, item):
        self._items.remove(item)

    def __iter__(self):
        return iter(self._items)


class _FakeResource(_ThreadChecked):
    def __init__(self, factory) -> None:
        super().__init__(factory)
        self.UniqueID = 31
        self.ID = 31
        self.Name = "Engineer"
        self.Type = 0
        self.MaxUnits = 1.0
        self.StandardRate = 125
        self.OvertimeRate = 180
        self.CostPerUse = 10
        self.MaterialLabel = ""
        self.AccrueAt = 3
        self.Initials = ""
        self.Group = ""
        self.Code = ""
        self.EMailAddress = ""
        self.Notes = ""
        self.BaseCalendar = "Standard"
        self.Calendar = None
        self.Overallocated = False

    def __setattr__(self, name, value):
        resource_type = self.__dict__.get("Type", 0)
        if name == "OvertimeRate" and resource_type != 0:
            raise RuntimeError("Project error 1101: overtime rate is work-resource only")
        if name == "MaterialLabel" and resource_type != 1 and value not in (None, ""):
            raise RuntimeError("Project error 1101: material label is material-resource only")
        if resource_type == 2 and name in {"MaxUnits", "StandardRate", "CostPerUse"}:
            raise RuntimeError("Project error 1101: rate fields are invalid for cost resources")
        super().__setattr__(name, value)
        if name == "BaseCalendar" and hasattr(self, "_project"):
            calendar = next(
                (item for item in self._project.BaseCalendars if item.Name == value),
                None,
            )
            super().__setattr__("Calendar", calendar)

    def Delete(self) -> None:
        self._project.Resources.remove(self)


class _FakeResources(_ThreadChecked):
    def __init__(self, factory, project) -> None:
        super().__init__(factory)
        self._project = project
        self._items = []

    def Add(self, name: str):
        resource = _FakeResource(self._factory)
        resource.UniqueID = max((item.UniqueID for item in self._items), default=30) + 1
        resource.ID = resource.UniqueID
        resource.Name = name
        object.__setattr__(resource, "_project", self._project)
        self._items.append(resource)
        self._project.Saved = False
        return resource

    def remove(self, item):
        self._items.remove(item)
        self._project.Saved = False

    def __iter__(self):
        return iter(self._items)


class _FakeTimeScaleValue(_ThreadChecked):
    def __init__(self, factory, assignment, key, *, mismatch: bool) -> None:
        super().__init__(factory)
        object.__setattr__(self, "_assignment", assignment)
        object.__setattr__(self, "_key", key)
        object.__setattr__(self, "_mismatch", mismatch)

    @property
    def Value(self):
        value = self._assignment._timephased_actual_work.get(self._key, 0)
        return value + 1 if self._mismatch else value

    @Value.setter
    def Value(self, value):
        self._assignment._timephased_actual_work[self._key] = int(value)
        self._assignment.ActualWork = sum(self._assignment._timephased_actual_work.values())
        self._assignment._project.Saved = False


class _FakeTimeScaleValues(_ThreadChecked):
    def __init__(self, factory, value) -> None:
        super().__init__(factory)
        self._value = value

    def Item(self, index: int):
        self._factory.timescale_item_calls.append(index)
        if index != 1:
            raise IndexError(index)
        return self._value

    def __iter__(self):
        return iter((self._value,))


class _FakeAssignment(_ThreadChecked):
    def __init__(self, factory, task, resource) -> None:
        super().__init__(factory)
        self.UniqueID = 41
        self.Task = task
        self.Resource = resource
        self.Units = 0.8
        self.Work = 480
        self.ActualWork = 0
        self.CostRateTable = 0
        self.Cost = 0
        object.__setattr__(self, "_timephased_actual_work", {})
        object.__setattr__(self, "_timephased_calls", {})

    def TimeScaleData(self, start, end, data_type, timescale_unit, count):
        key = start.date().isoformat()
        calls = self._timephased_calls.get(key, 0) + 1
        self._timephased_calls[key] = calls
        self._factory.timephased_calls.append(
            (self.UniqueID, start, end, data_type, timescale_unit, count)
        )
        value = _FakeTimeScaleValue(
            self._factory,
            self,
            key,
            mismatch=self._factory.force_timephased_reread_mismatch and calls % 2 == 0,
        )
        return _FakeTimeScaleValues(self._factory, value)

    def Delete(self) -> None:
        self._project.Assignments.remove(self)


class _FakeAssignments(_ThreadChecked):
    def __init__(self, factory, project) -> None:
        super().__init__(factory)
        self._project = project
        self._items = []

    def Add(self, task_id: int, resource_id: int, units=None):
        task = next(item for item in self._project.Tasks if item.ID == task_id)
        resource = next(item for item in self._project.Resources if item.ID == resource_id)
        assignment = _FakeAssignment(self._factory, task, resource)
        assignment.UniqueID = max((item.UniqueID for item in self._items), default=40) + 1
        self._factory.assignment_add_units.append(units)
        if units is not None:
            assignment.Units = units / 100.0 if self._factory.assignment_units_as_percentage else units
        object.__setattr__(assignment, "_project", self._project)
        self._items.append(assignment)
        self._project.Saved = False
        return assignment

    def remove(self, item):
        self._items.remove(item)
        self._project.Saved = False

    def __iter__(self):
        return iter(self._items)


class _ProjectAssignmentsTrap(_FakeAssignments):
    def Add(self, *args, **kwargs):
        raise AssertionError("Project.Assignments.Add must not be used")


class _FakeShift(_ThreadChecked):
    def __init__(self, factory) -> None:
        super().__init__(factory)
        self.Start = None
        self.Finish = None


class _FakeWeekDay(_ThreadChecked):
    def __init__(self, factory, index: int) -> None:
        super().__init__(factory)
        self.Index = index
        self.Working = False
        for shift in range(1, 6):
            setattr(self, f"Shift{shift}", _FakeShift(factory))


class _FakeException(_ThreadChecked):
    def __init__(self, factory, collection, start, finish, name) -> None:
        super().__init__(factory)
        self._collection = collection
        self.Name = name
        self.Start = start
        self.Finish = finish
        for shift in range(1, 6):
            setattr(self, f"Shift{shift}", _FakeShift(factory))

    def Delete(self):
        self._collection._items.remove(self)


class _FakeExceptions(_ThreadChecked):
    def __init__(self, factory) -> None:
        super().__init__(factory)
        self._items = []

    def Add(self, *, Type, Start, Finish, Name):
        item = _FakeException(self._factory, self, Start, Finish, Name)
        self._items.append(item)
        return item

    def __iter__(self):
        return iter(self._items)


class _FakeCalendar(_ThreadChecked):
    def __init__(self, factory) -> None:
        super().__init__(factory)
        self.GUID = "calendar-guid-1"
        self.Name = "Standard"
        self.BaseCalendar = None
        self.WeekDays = [_FakeWeekDay(factory, index) for index in range(1, 8)]
        self.Exceptions = _FakeExceptions(factory)


class _FakeProjects(_ThreadChecked):
    def __init__(self, factory, app, kind: str) -> None:
        super().__init__(factory)
        self._app = app
        self._kind = kind
        self._items = []

    def Add(self, *args):
        self._factory.project_add_calls.append(args)
        project = _FakeProject(self._factory, self._kind, name="Project", full_name="")
        object.__setattr__(project, "_app", self._app)
        self._items.append(project)
        self._app.ActiveProject = project
        return project

    def __iter__(self):
        return iter(self._items)


class _FakeProject(_ThreadChecked):
    def __init__(self, factory, kind: str, *, name: str, full_name: str) -> None:
        super().__init__(factory)
        factory.project_count += 1
        self._kind = kind
        self.Name = name
        self.FullName = full_name
        self.Saved = False
        self.ProjectGUID = f"native-{kind}-guid-{factory.project_count}"
        self.UniqueID = 999
        self.ProjectStart = datetime(2027, 1, 1, 8, 0)
        self.ProjectFinish = datetime(2027, 1, 31, 17, 0)
        self.ScheduleFromStart = True
        self.CurrentDate = datetime(2027, 1, 1, 8, 0)
        self.DefaultTaskType = 0
        self.DefaultEffortDriven = False
        self.NewTasksCreatedAsManual = False
        self.HonorConstraints = True
        self.MultipleCriticalPaths = False
        self.HoursPerDay = 8
        self.HoursPerWeek = 40
        self.DaysPerMonth = 20
        self.Title = "Program"
        self.Manager = "Ada"
        self.Company = "Example"
        self.Subject = "Delivery"
        self.Author = ""
        self.Keywords = ""
        self.ProjectNotes = ""
        self.StatusDate = datetime(2027, 1, 2, 17, 0)
        root = _FakeTask(factory, 11, "Root")
        child = _FakeTask(factory, 22, "Child", root)
        resource = _FakeResource(factory)
        self.Tasks = _FakeTasks(factory, self)
        self.Tasks._items.extend((root, child))
        self.ProjectSummaryTask = root
        self.Tasks._reindex()
        for task in (root, child):
            object.__setattr__(task, "_project", self)
            task.TaskDependencies = _FakeTaskDependencies(factory, self, task)
        self.Resources = _FakeResources(factory, self)
        self.Resources._items.append(resource)
        object.__setattr__(resource, "_project", self)
        self.Assignments = _FakeAssignments(factory, self)
        root.Assignments = self.Assignments
        child.Assignments = self.Assignments
        assignment = _FakeAssignment(factory, child, resource)
        object.__setattr__(assignment, "_project", self)
        self.Assignments._items.append(assignment)
        self.Calendars = [_FakeCalendar(factory)]
        self.BaseCalendars = self.Calendars
        self.Calendar = self.Calendars[0]
        dependency = _FakeDependency(factory, root, child)
        object.__setattr__(dependency, "_project", self)
        self.Dependencies = [dependency]
        child.TaskDependencies._items.append(dependency)
        if factory.omit_explicit_baselines:
            for task in (root, child):
                task.BaselineStart = "NA"
                for baseline in range(1, 11):
                    setattr(task, f"Baseline{baseline}Start", "NA")
        else:
            self.Baselines = [0]

    def __setattr__(self, name, value):
        if name == "Name" and "Name" in self.__dict__:
            raise AttributeError("Project.Name is read-only")
        super().__setattr__(name, value)

    def SaveAs(self, path: str):
        if not self._factory.save_as_result:
            return False
        self.FullName = path
        self.Saved = True
        return True

    def Save(self) -> None:
        if not self.FullName:
            self._factory.modal_prompt_attempted = True
            raise RuntimeError("modal Save As prompt would open")
        self.Saved = True

    def Close(self, save_changes: bool = True) -> None:
        if save_changes:
            self.Saved = True
        self._factory.closed_documents.append(self._kind)
        object.__setattr__(self, "_closed", True)

    def Activate(self) -> None:
        self._app.ActiveProject = self

    def ExportAsFixedFormat(self, path: str, file_type: int) -> None:
        self._factory.export_calls.append((path, file_type))
        with open(path, "wb") as stream:
            stream.write(b"%PDF-fake")

    def snapshot(self):
        task_fields = (
            "Name", "Duration", "Milestone", "PercentComplete", "ActualDuration", "RemainingDuration",
            "ActualWork", "RemainingWork", "ActualStart", "ActualFinish", "FixedCost", "FixedCostAccrual",
            "ConstraintType", "ConstraintDate", "Deadline", "Type", "EffortDriven", "Manual",
            "Priority", "Notes", "Calendar", "CalendarObject", "IgnoreResourceCalendar",
        )
        resource_fields = (
            "Name", "Type", "MaxUnits", "StandardRate", "OvertimeRate", "CostPerUse", "MaterialLabel",
            "AccrueAt", "Initials", "Group", "Code", "EMailAddress", "Notes", "BaseCalendar", "Calendar",
        )
        assignment_fields = ("Units", "Work", "ActualWork", "CostRateTable", "Cost")
        return {
            "saved": self.Saved,
            "project": {
                key: copy.deepcopy(getattr(self, key))
                for key in (
                    "Title", "Manager", "Company", "Subject", "Author", "Keywords", "ProjectNotes",
                    "ProjectStart", "ProjectFinish", "ScheduleFromStart", "CurrentDate", "Calendar",
                    "DefaultTaskType", "DefaultEffortDriven", "NewTasksCreatedAsManual",
                    "HonorConstraints", "MultipleCriticalPaths", "HoursPerDay", "HoursPerWeek",
                    "DaysPerMonth", "StatusDate", "Baselines",
                )
                if key in self.__dict__
            },
            "tasks": list(self.Tasks._items),
            "task_values": [(item, {key: copy.deepcopy(getattr(item, key)) for key in task_fields}) for item in self.Tasks],
            "resources": list(self.Resources._items),
            "resource_values": [(item, {key: copy.deepcopy(getattr(item, key)) for key in resource_fields}) for item in self.Resources],
            "assignments": list(self.Assignments._items),
            "assignment_values": [(item, {key: copy.deepcopy(getattr(item, key)) for key in assignment_fields}) for item in self.Assignments],
            "assignment_timephased": [
                (item, copy.deepcopy(item._timephased_actual_work)) for item in self.Assignments
            ],
            "dependencies": list(self.Dependencies),
        }

    def restore(self, snapshot) -> None:
        self.Saved = snapshot["saved"]
        for key, value in snapshot["project"].items():
            setattr(self, key, value)
        self.Tasks._items[:] = snapshot["tasks"]
        for item, values in snapshot["task_values"]:
            for key, value in values.items():
                setattr(item, key, value)
        self.Resources._items[:] = snapshot["resources"]
        for item, values in snapshot["resource_values"]:
            for key, value in values.items():
                setattr(item, key, value)
        self.Assignments._items[:] = snapshot["assignments"]
        for item, values in snapshot["assignment_values"]:
            for key, value in values.items():
                setattr(item, key, value)
        for item, values in snapshot["assignment_timephased"]:
            item._timephased_actual_work.clear()
            item._timephased_actual_work.update(values)
        self.Dependencies[:] = snapshot["dependencies"]

    def simulate_task_edit(self, unique_id: int, name: str) -> None:
        for task in self.Tasks:
            if task.UniqueID == unique_id:
                task.Name = name
        self.Saved = False

    def simulate_reorder(self) -> None:
        self.Tasks._items.reverse()

    def simulate_full_name_change(self, path: str) -> None:
        self.FullName = path

    def simulate_close(self) -> None:
        object.__setattr__(self, "_closed", True)


class _FakeApplication(_ThreadChecked):
    def __init__(self, factory, kind: str) -> None:
        super().__init__(factory)
        self._kind = kind
        self.Projects = _FakeProjects(factory, self, kind)
        self.ActiveProject = None
        self.Visible = False
        self._undo_snapshot = None

    def _record_scope(self, operation: str) -> None:
        project = self.ActiveProject
        self._factory.application_scopes.append(
            (operation, None if project is None else project.ProjectGUID)
        )

    def OpenUndoTransaction(self, label: str):
        self._record_scope("open_undo")
        self._factory.undo_open_count += 1
        self._factory.undo_labels.append(label)
        self._undo_snapshot = self.ActiveProject.snapshot()

    def CloseUndoTransaction(self):
        self._record_scope("close_undo")
        self._factory.undo_close_count += 1

    def Undo(self):
        self._record_scope("undo")
        self._factory.undo_count += 1
        if self._factory.fail_undo:
            raise RuntimeError("undo failed")
        self.ActiveProject.restore(self._undo_snapshot)

    def CalculateProject(self):
        self._record_scope("calculate")
        self._factory.calculate_count += 1
        if self._factory.fail_calculate:
            raise RuntimeError("calculation failed")
        if self._factory.force_reread_mismatch:
            next(iter(self.ActiveProject.Tasks)).Duration += 1
        created = self._factory.last_created_task
        if created is not None and self._factory.force_created_parent_mismatch:
            created.OutlineParent = None
            created.OutlineLevel = 1
        if created is not None and self._factory.force_created_root_parent_proxy:
            created.OutlineParent = _FakeTask(self._factory, 0, "Project summary proxy")
            created.OutlineLevel = 1
        if created is not None and self._factory.force_created_row_mismatch:
            tasks = self.ActiveProject.Tasks
            tasks._items.remove(created)
            tasks._items.append(created)
            tasks._reindex()
        return self._factory.calculate_result

    @property
    def ActiveCell(self):
        self._factory.selection_api_calls.append("ActiveCell")
        return None

    def SelectTaskField(self, *args, **kwargs):
        self._factory.selection_api_calls.append("SelectTaskField")

    def SelectRow(self, *args, **kwargs):
        self._factory.selection_api_calls.append("SelectRow")

    def OutlineIndent(self, *args, **kwargs):
        self._factory.selection_api_calls.append("Application.OutlineIndent")

    def LevelNow(self, all_tasks: bool):
        self._record_scope("level")
        self._factory.level_attempts += 1
        if self._factory.level_attempts <= self._factory.busy_level_failures:
            error = RuntimeError("Project is busy")
            error.hresult = -2147418111
            raise error
        self._factory.schedule_calls.append(("level", all_tasks))
        return self._factory.level_result

    def LevelingClear(self, all_tasks: bool):
        self._record_scope("clear_leveling")
        self._factory.schedule_calls.append(("clear", all_tasks))
        return self._factory.leveling_clear_result

    def UpdateProject(self, all_tasks: bool, date_value, reschedule: int):
        self._record_scope("reschedule")
        self._factory.schedule_calls.append(("reschedule", all_tasks, date_value, reschedule))
        return self._factory.update_project_result

    def ProjectSummaryInfo(self, *args, **kwargs):
        if kwargs:
            raise AssertionError("ProjectSummaryInfo must use positional arguments")
        self._factory.project_summary_calls.append(args)
        keys = (
            "Project", "Title", "Subject", "Author", "Company", "Manager", "Keywords",
            "Comments", "Start", "Finish", "ScheduleFrom", "CurrentDate", "Calendar",
            "StatusDate", "Priority",
        )
        kwargs.update((key, value) for key, value in zip(keys, args) if key != "Project")
        mapping = {
            "Title": "Title", "Subject": "Subject", "Author": "Author", "Company": "Company",
            "Manager": "Manager", "Keywords": "Keywords", "Comments": "ProjectNotes",
            "Start": "ProjectStart", "Finish": "ProjectFinish", "CurrentDate": "CurrentDate",
        }
        for key, target in mapping.items():
            if key in kwargs:
                setattr(self.ActiveProject, target, kwargs[key])
        if "ScheduleFrom" in kwargs:
            self.ActiveProject.ScheduleFromStart = kwargs["ScheduleFrom"] == 1
        if "Calendar" in kwargs:
            self.ActiveProject.Calendar = next(
                item for item in self.ActiveProject.BaseCalendars if item.Name == kwargs["Calendar"]
            )
        if "Priority" in kwargs:
            self.ActiveProject.ProjectSummaryTask.Priority = kwargs["Priority"]
        self.ActiveProject.Saved = False

    def BaselineSave(self, all_tasks, copy_from, into):
        baseline = 0 if into == 0 else into - 10
        if "Baselines" in self.ActiveProject.__dict__:
            if baseline not in self.ActiveProject.Baselines:
                self.ActiveProject.Baselines.append(baseline)
        else:
            field = "BaselineStart" if baseline == 0 else f"Baseline{baseline}Start"
            for task in self.ActiveProject.Tasks:
                setattr(task, field, task.Start)
        self.ActiveProject.Saved = False

    def BaselineClear(self, all_tasks, from_baseline):
        baseline = 0 if from_baseline == 0 else from_baseline - 10
        if "Baselines" in self.ActiveProject.__dict__:
            if baseline in self.ActiveProject.Baselines:
                self.ActiveProject.Baselines.remove(baseline)
        else:
            field = "BaselineStart" if baseline == 0 else f"Baseline{baseline}Start"
            for task in self.ActiveProject.Tasks:
                setattr(task, field, "NA")
        self.ActiveProject.Saved = False

    def BaseCalendarCreate(self, *, Name, FromName=None):
        calendar = _FakeCalendar(self._factory)
        calendar.Name = Name
        calendar.GUID = f"calendar-guid-{len(self.ActiveProject.Calendars) + 1}"
        if FromName is not None:
            calendar.BaseCalendar = next(
                item for item in self.ActiveProject.BaseCalendars if item.Name == FromName
            )
        self.ActiveProject.Calendars.append(calendar)
        self.ActiveProject.Saved = False
        return True

    def BaseCalendarRename(self, *, FromName, ToName):
        calendar = next(item for item in self.ActiveProject.BaseCalendars if item.Name == FromName)
        calendar.Name = ToName
        self.ActiveProject.Saved = False
        return True

    def BaseCalendarDelete(self, *, Name):
        calendar = next(item for item in self.ActiveProject.BaseCalendars if item.Name == Name)
        self.ActiveProject.Calendars.remove(calendar)
        self.ActiveProject.Saved = False
        return True

    def BaseCalendarEditDays(self, **kwargs):
        calendar = next(item for item in self.ActiveProject.BaseCalendars if item.Name == kwargs["Name"])
        day = next(item for item in calendar.WeekDays if item.Index == kwargs["WeekDay"])
        day.Working = kwargs["Working"]
        for index in range(1, 6):
            shift = getattr(day, f"Shift{index}")
            shift.Start = kwargs.get(f"From{index}")
            shift.Finish = kwargs.get(f"To{index}")
        self.ActiveProject.Saved = False
        return True

    def FileOpen(self, path: str):
        project = _FakeProject(self._factory, self._kind, name="Opened", full_name=path)
        object.__setattr__(project, "_app", self)
        project.Saved = True
        self.Projects._items.append(project)
        self.ActiveProject = project
        return True

    def FileCloseEx(self, save_type: int):
        self._factory.file_close_types.append((self._kind, save_type))
        if not self._factory.file_close_result:
            return False
        project = self.ActiveProject
        if project is None:
            return False
        if save_type == 1:
            project.Saved = True
        self._factory.closed_documents.append(self._kind)
        object.__setattr__(project, "_closed", True)
        self.ActiveProject = None
        return True

    def Quit(self) -> None:
        if self._kind == "server":
            self._factory.server_quit_count += 1
        else:
            self._factory.user_quit_count += 1


class _FakeAutomationFactory:
    def __init__(self) -> None:
        self.owner_thread_id = None
        self.access_threads: list[int] = []
        self.provider_calls = 0
        self.create_calls = 0
        self.attach_calls = 0
        self.server_app = None
        self.user_app = None
        self.server_quit_count = 0
        self.user_quit_count = 0
        self.closed_documents: list[str] = []
        self.calculate_count = 0
        self.undo_open_count = 0
        self.undo_close_count = 0
        self.undo_count = 0
        self.undo_labels: list[str] = []
        self.fail_calculate = False
        self.calculate_result = True
        self.fail_undo = False
        self.force_reread_mismatch = False
        self.force_timephased_reread_mismatch = False
        self.timephased_calls: list[tuple] = []
        self.timescale_item_calls: list[int] = []
        self.task_add_calls: list[tuple[str, int | None]] = []
        self.outline_indent_calls: list[int] = []
        self.outline_outdent_calls: list[int] = []
        self.last_created_task = None
        self.force_created_parent_mismatch = False
        self.force_created_root_parent_proxy = False
        self.force_created_row_mismatch = False
        self.selection_api_calls: list[str] = []
        self.export_calls: list[tuple[str, int]] = []
        self.schedule_calls: list[tuple] = []
        self.busy_level_failures = 0
        self.level_attempts = 0
        self.level_result = True
        self.leveling_clear_result = True
        self.update_project_result = True
        self.global_option = "unchanged"
        self.file_close_types: list[tuple[str, int]] = []
        self.assignment_units_as_percentage = False
        self.assignment_add_units: list[float | None] = []
        self.project_summary_calls: list[tuple] = []
        self.project_add_calls: list[tuple] = []
        self.omit_explicit_baselines = False
        self.modal_prompt_attempted = False
        self.save_as_result = True
        self.file_close_result = True
        self.application_scopes: list[tuple[str, str | None]] = []
        self.project_count = 0

    def record_access(self) -> None:
        thread_id = threading.get_ident()
        if self.owner_thread_id is None:
            self.owner_thread_id = thread_id
        if thread_id != self.owner_thread_id:
            raise AssertionError("fake COM object was accessed outside its STA owner")
        self.access_threads.append(thread_id)

    def provider(self):
        self.record_access()
        self.provider_calls += 1
        return self

    def create_application(self):
        self.record_access()
        self.create_calls += 1
        if self.server_app is None:
            self.server_app = _FakeApplication(self, "server")
        return self.server_app

    def get_active_application(self):
        self.record_access()
        self.attach_calls += 1
        if self.user_app is None:
            self.user_app = _FakeApplication(self, "user")
            project = _FakeProject(self, "user", name="User Plan", full_name="C:\\Plans\\User.mpp")
            object.__setattr__(project, "_app", self.user_app)
            project.Saved = True
            self.user_app.Projects._items.append(project)
            self.user_app.ActiveProject = project
        return self.user_app


def _ready_detection() -> DesktopProjectDetection:
    return DesktopProjectDetection(
        platform="Windows",
        windows=True,
        com_registered=True,
        prog_ids=("MSProject.Application",),
        clsids=("{PROJECT}",),
        pywin32_importable=True,
        pythoncom_importable=True,
        win32com_importable=True,
    )


class LiveProjectBackendTests(unittest.TestCase):
    def _backend(self, *, omit_explicit_baselines: bool = False):
        factory = _FakeAutomationFactory()
        factory.omit_explicit_baselines = omit_explicit_baselines
        host = StaHost(runtime_factory=lambda: _NoopRuntime(), pump_interval=0.005)
        backend = LiveProjectBackend(
            detection=_ready_detection(),
            sta_host=host,
            automation_factory_provider=factory.provider,
            call_timeout=2,
        )
        return backend, factory, host

    @staticmethod
    def _discard_and_shutdown(backend, project) -> None:
        backend.close_project(
            project,
            CloseDisposition.DISCARD_AND_CLOSE,
            expected_state=backend.current_state(project),
        )
        backend.shutdown()

    def test_import_and_capabilities_do_not_activate_or_start_sta(self) -> None:
        factory = _FakeAutomationFactory()
        host = StaHost(runtime_factory=lambda: _NoopRuntime(), pump_interval=0.005)
        backend = LiveProjectBackend(
            detection=_ready_detection(),
            sta_host=host,
            automation_factory_provider=factory.provider,
        )
        capabilities = backend.capabilities()
        self.assertTrue(capabilities.available)
        self.assertFalse(capabilities.activates_desktop)
        self.assertIn("create_task:parent_after", capabilities.supported_operations)
        self.assertIn("delete_task:non_recursive", capabilities.supported_operations)
        self.assertIn("delete_task:recursive", capabilities.supported_operations)
        self.assertIn("status:timephased_actual_work_daily", capabilities.supported_operations)
        self.assertNotIn("move_task", capabilities.supported_operations)
        self.assertEqual(host.state, StaHostState.NEW)
        self.assertEqual(factory.provider_calls, 0)
        with self.assertRaises(MspError) as unsupported:
            backend.schedule(
                None,
                ScheduleCommand.CALCULATE,
                ScheduleOptions(),
                expected_state=ProjectState(token="sha256:" + "0" * 64),
            )
        self.assertEqual(unsupported.exception.code, ErrorCode.SESSION_NOT_FOUND)
        self.assertEqual(host.state, StaHostState.NEW)
        backend.shutdown()

    def test_invalid_create_paths_reject_before_project_activation(self) -> None:
        backend, factory, host = self._backend()
        with tempfile.TemporaryDirectory() as directory:
            existing = os.path.join(directory, "existing.mpp")
            with open(existing, "wb") as stream:
                stream.write(b"existing")
            invalid = (
                "relative.mpp",
                os.path.join(directory, "missing", "plan.mpp"),
                existing,
                os.path.join(directory, "wrong.txt"),
            )
            for path in invalid:
                with self.assertRaises(MspError):
                    backend.create_project(name="Invalid", path=path)
            service = ProjectService(
                backend,
                ledger=InMemoryOperationLedger(),
                confirmation_secret=b"invalid-path-lifecycle-secret",
            )
            request = ProjectRequest(
                action=ProjectAction.CREATE,
                name="Invalid",
                path=os.path.join(directory, "missing", "plan.mpp"),
                idempotency_key="invalid-create-path-0001",
            )
            for _ in range(2):
                with self.assertRaises(MspError):
                    service.project(request)
            self.assertIsNone(service.ledger.lookup("msp-lifecycle", request.idempotency_key))
        self.assertEqual(factory.provider_calls, 0)
        self.assertEqual(factory.create_calls, 0)
        self.assertEqual(host.state, StaHostState.NEW)
        backend.shutdown()

    def test_invalid_open_paths_reject_before_project_activation(self) -> None:
        backend, factory, host = self._backend()
        invalid = ("relative.mpp", os.path.join(tempfile.gettempdir(), "missing-project.mpp"))
        for path in invalid:
            with self.assertRaises(MspError):
                backend.open_project(path=path)
        self.assertEqual(factory.provider_calls, 0)
        backend.shutdown()

    def test_create_from_template_uses_the_documented_projects_add_arguments(self) -> None:
        backend, factory, _ = self._backend()
        with tempfile.TemporaryDirectory() as directory:
            template = os.path.join(directory, "delivery.mpt")
            with open(template, "wb") as stream:
                stream.write(b"fake-template")
            session = backend.create_project(
                name="From template",
                path=None,
                template_path=template,
            )
            self.assertEqual(
                factory.project_add_calls[-1],
                (False, os.path.realpath(template), False),
            )
            self._discard_and_shutdown(backend, session.project)

    def test_live_read_boundary_is_stable_and_detects_ui_state_changes(self) -> None:
        backend, factory, host = self._backend()
        session = backend.create_project(name="Launch", path=None)
        self.assertTrue(host.call(lambda: factory.server_app.Visible))
        self.assertEqual(session.ownership, Ownership.SERVER_OWNED)
        self.assertEqual(session.name, "Launch")
        self.assertEqual(host.call(lambda: factory.server_app.ActiveProject.Name), "Project")
        self.assertEqual(host.call(lambda: factory.server_app.ActiveProject.Title), "Launch")
        initial = backend.current_state(session.project)
        tasks = backend.query(session.project, QueryEntity.TASK, fields=(), limit=100, offset=0)
        self.assertEqual([item["ref"]["unique_id"] for item in tasks.items], [11, 22])
        self.assertEqual(backend.dependency_edges(session.project), ((11, 22),))
        self.assertEqual(backend.task_parent_edges(session.project), ((22, 11),))
        self.assertEqual(
            backend.resolve_ref(session.project, ObjectRef(kind=ObjectKind.CALENDAR, guid="calendar-guid-1")),
            "calendar-guid-1",
        )

        host.call(lambda: factory.server_app.ActiveProject.simulate_reorder())
        reordered = backend.query(session.project, QueryEntity.TASK, fields=(), limit=100, offset=0)
        self.assertEqual([item["ref"]["unique_id"] for item in reordered.items], [11, 22])
        host.call(lambda: factory.server_app.ActiveProject.simulate_task_edit(22, "Edited in UI"))
        changed = backend.current_state(session.project)
        self.assertNotEqual(changed, initial)
        with self.assertRaises(MspError) as unknown:
            backend.query(session.project, QueryEntity.TASK, fields=("row_id",), limit=10, offset=0)
        self.assertEqual(unknown.exception.code, ErrorCode.INVALID_REQUEST)
        self.assertEqual(set(factory.access_threads), {host.owner_thread_id})

        backend.save_project(
            session.project,
            path=os.path.join(tempfile.gettempdir(), "fake-launch.mpp"),
            expected_state=changed,
        )
        backend.close_project(
            session.project,
            CloseDisposition.REFUSE_IF_DIRTY,
            expected_state=backend.current_state(session.project),
        )
        backend.shutdown()
        self.assertEqual(factory.server_quit_count, 1)
        self.assertEqual(factory.user_quit_count, 0)
        self.assertIn(("server", 0), factory.file_close_types)

    def test_project_summary_info_uses_positional_calls(self) -> None:
        backend, factory, _ = self._backend()
        session = backend.create_project(name="Positional title", path=None)
        self.assertEqual(
            factory.project_summary_calls,
            [("Project", "Positional title")],
        )
        self._discard_and_shutdown(backend, session.project)

    def test_advanced_planning_fields_use_object_scoped_com_and_reread(self) -> None:
        backend, _, _ = self._backend()
        session = backend.create_project(name="Advanced planning", path=None)
        start = datetime(2027, 2, 1, 8, 0)
        deadline = datetime(2027, 2, 12, 17, 0)
        receipt = backend.apply_operations(
            session.project,
            (
                CreateCalendar(client_ref="delivery-calendar", name="Delivery Calendar"),
                CreateTask(
                    client_ref="controlled-task",
                    name="Controlled task",
                    constraint_type=TaskConstraintType.START_NO_EARLIER_THAN,
                    constraint_date=start,
                    deadline=deadline,
                    task_type=TaskType.FIXED_DURATION,
                    effort_driven=False,
                    manual=False,
                    priority=700,
                    notes="Release gate",
                    calendar=ObjectRef(kind=ObjectKind.CALENDAR, client_ref="delivery-calendar"),
                    ignore_resource_calendar=True,
                ),
                CreateResource(
                    client_ref="lead",
                    name="Delivery Lead",
                    initials="DL",
                    group="Delivery",
                    code="DL-01",
                    email="lead@example.com",
                    notes="Primary owner",
                    base_calendar=ObjectRef(kind=ObjectKind.CALENDAR, client_ref="delivery-calendar"),
                ),
                UpdateProjectProperties(
                    calendar=ObjectRef(kind=ObjectKind.CALENDAR, client_ref="delivery-calendar"),
                    default_task_type=TaskType.FIXED_DURATION,
                    default_effort_driven=False,
                    new_tasks_manual=False,
                    honor_constraints=True,
                    multiple_critical_paths=True,
                ),
            ),
            idempotency_key="advanced-planning-0001",
            verification=VerificationLevel.NATIVE_REREAD,
            expected_state=session.state,
        )
        self.assertTrue(all(item["verified"] for item in receipt.observed))
        task = next(
            item
            for item in backend.query(
                session.project, QueryEntity.TASK, fields=(), limit=100, offset=0
            ).items
            if item["name"] == "Controlled task"
        )
        resource = next(
            item
            for item in backend.query(
                session.project, QueryEntity.RESOURCE, fields=(), limit=100, offset=0
            ).items
            if item["name"] == "Delivery Lead"
        )
        self.assertEqual(task["constraint_type_name"], TaskConstraintType.START_NO_EARLIER_THAN.value)
        self.assertEqual(task["calendar_ref"]["guid"], "calendar-guid-2")
        self.assertEqual(resource["base_calendar_ref"]["guid"], "calendar-guid-2")
        self._discard_and_shutdown(backend, session.project)

    def test_assignments_use_task_collections(self) -> None:
        backend, factory, host = self._backend()
        session = backend.create_project(name="Task-owned assignments", path=None)

        def reject_project_assignment_collection() -> None:
            project = factory.server_app.ActiveProject
            shared = project.Assignments
            project.Assignments = _ProjectAssignmentsTrap(factory, project)
            for task in project.Tasks:
                task.Assignments = shared

        host.call(reject_project_assignment_collection)
        initial = backend.query(
            session.project, QueryEntity.ASSIGNMENT, fields=(), limit=100, offset=0
        ).items
        self.assertEqual([item["ref"]["unique_id"] for item in initial], [41])

        receipt = backend.apply_operations(
            session.project,
            (
                CreateAssignment(
                    client_ref="task-owned",
                    task=ObjectRef(kind=ObjectKind.TASK, unique_id=22),
                    resource=ObjectRef(kind=ObjectKind.RESOURCE, unique_id=31),
                    units_percent=50,
                ),
            ),
            idempotency_key="task-owned-assignment-0001",
            verification=VerificationLevel.NATIVE_REREAD,
            expected_state=session.state,
        )
        self.assertTrue(receipt.observed[0]["verified"])
        self._discard_and_shutdown(backend, session.project)

    def test_created_root_outline_parent_proxy_unique_id_zero_verifies_as_no_parent(self) -> None:
        backend, factory, _ = self._backend()
        session = backend.create_project(name="Root proxy", path=None)
        factory.force_created_root_parent_proxy = True
        receipt = backend.apply_operations(
            session.project,
            (CreateTask(client_ref="root-with-proxy", name="Top-level task"),),
            idempotency_key="root-outline-parent-proxy-0001",
            verification=VerificationLevel.NATIVE_REREAD,
            expected_state=session.state,
        )
        self.assertTrue(receipt.observed[0]["verified"])
        tasks = backend.query(session.project, QueryEntity.TASK, fields=(), limit=100, offset=0)
        root = next(item for item in tasks.items if item["name"] == "Top-level task")
        self.assertIsNone(root["parent_ref"])
        self.assertEqual(backend.task_parent_edges(session.project), ((22, 11),))
        self._discard_and_shutdown(backend, session.project)

    def test_numeric_enum_and_percent_mappings_are_publicly_stable(self) -> None:
        backend, _, _ = self._backend()
        session = backend.create_project(name="Mappings", path=None)
        dependency = backend.query(session.project, QueryEntity.DEPENDENCY, fields=(), limit=10, offset=0).items[0]
        resource = backend.query(session.project, QueryEntity.RESOURCE, fields=(), limit=10, offset=0).items[0]
        assignment = backend.query(session.project, QueryEntity.ASSIGNMENT, fields=(), limit=10, offset=0).items[0]
        task = backend.query(session.project, QueryEntity.TASK, fields=(), limit=10, offset=0).items[0]
        self.assertEqual(dependency["dependency_type"], "FS")
        self.assertEqual(resource["resource_type"], "work")
        self.assertEqual(resource["max_units_percent"], 100)
        self.assertEqual(assignment["units_percent"], 80)
        self.assertEqual(assignment["cost_rate_table"], "A")
        self.assertEqual(task["cost_accrual"], "prorated")
        self._discard_and_shutdown(backend, session.project)

    def test_non_recursive_delete_rejects_summary_before_undo_or_cascade(self) -> None:
        backend, factory, host = self._backend()
        session = backend.create_project(name="Delete Guard", path=None)
        with self.assertRaises(MspError) as rejected:
            backend.apply_operations(
                session.project,
                (DeleteTask(task=ObjectRef(kind=ObjectKind.TASK, unique_id=11), recursive=False),),
                idempotency_key="summary-delete-guard-0001",
                verification=VerificationLevel.NATIVE_REREAD,
                expected_state=session.state,
            )
        self.assertEqual(rejected.exception.code, ErrorCode.UNSUPPORTED_OPERATION)
        self.assertEqual(factory.undo_open_count, 0)
        remaining = host.call(
            lambda: [item.UniqueID for item in factory.server_app.ActiveProject.Tasks]
        )
        self.assertEqual(remaining, [11, 22])
        self._discard_and_shutdown(backend, session.project)

    def test_recursive_delete_removes_and_verifies_the_summary_subtree(self) -> None:
        backend, _, _ = self._backend()
        session = backend.create_project(name="Recursive delete", path=None)
        receipt = backend.apply_operations(
            session.project,
            (DeleteTask(task=ObjectRef(kind=ObjectKind.TASK, unique_id=11), recursive=True),),
            idempotency_key="recursive-delete-0001",
            verification=VerificationLevel.NATIVE_REREAD,
            expected_state=session.state,
        )
        deleted = receipt.observed[0]["native"]["deleted_refs"]
        self.assertEqual([item["unique_id"] for item in deleted], [11, 22])
        tasks = backend.query(
            session.project, QueryEntity.TASK, fields=(), limit=100, offset=0
        )
        self.assertEqual(tasks.items, ())
        self._discard_and_shutdown(backend, session.project)

    def test_live_apply_full_supported_batch_calculates_once_and_rereads(self) -> None:
        backend, factory, host = self._backend()
        factory.assignment_units_as_percentage = True
        session = backend.create_project(name="Write", path=None)
        operations = (
            CreateTask(client_ref="new-task", name="New", duration_minutes=240),
            CreateResource(client_ref="new-resource", name="Analyst", max_units_percent=50),
            CreateAssignment(
                client_ref="new-assignment",
                task=ObjectRef(kind=ObjectKind.TASK, client_ref="new-task"),
                resource=ObjectRef(kind=ObjectKind.RESOURCE, client_ref="new-resource"),
                units_percent=25,
                work_minutes=120,
                cost_rate_table="B",
            ),
            AddDependency(
                predecessor=ObjectRef(kind=ObjectKind.TASK, unique_id=11),
                successor=ObjectRef(kind=ObjectKind.TASK, client_ref="new-task"),
                dependency_type="SS",
                lag_minutes=30,
            ),
            UpdateProjectProperties(title="Updated", comments="Native notes"),
            SetBaseline(baseline=1),
            CreateCalendar(
                client_ref="new-calendar",
                name="Delivery Calendar",
                weekly=(
                    WorkingDay(
                        weekday=Weekday.MONDAY,
                        intervals=(WorkingInterval(start=time(8), end=time(16)),),
                    ),
                ),
                exceptions=(
                    CalendarException(
                        name="Holiday",
                        start_date=date(2027, 12, 24),
                        end_date=date(2027, 12, 24),
                    ),
                ),
            ),
        )
        receipt = backend.apply_operations(
            session.project,
            operations,
            idempotency_key="live-batch-0001",
            verification=VerificationLevel.NATIVE_REREAD,
            expected_state=session.state,
        )
        self.assertEqual(factory.calculate_count, 1)
        self.assertEqual(factory.undo_open_count, 1)
        self.assertEqual(factory.undo_close_count, 1)
        self.assertEqual(factory.assignment_add_units, [None])
        self.assertTrue(all(item["verified"] for item in receipt.observed))
        self.assertNotEqual(receipt.state_before, receipt.state_after)
        assignments = backend.query(session.project, QueryEntity.ASSIGNMENT, fields=(), limit=100, offset=0).items
        self.assertIn(25, [item["units_percent"] for item in assignments])
        calendars = backend.query(session.project, QueryEntity.CALENDAR, fields=(), limit=100, offset=0).items
        self.assertIn("Delivery Calendar", [item["name"] for item in calendars])
        self.assertEqual(set(factory.access_threads), {host.owner_thread_id})
        self._discard_and_shutdown(backend, session.project)

    def test_resource_types_use_only_valid_fields_and_verify_rates(self) -> None:
        backend, _, _ = self._backend()
        session = backend.create_project(name="Resource Types", path=None)
        operations = (
            CreateResource(
                client_ref="material",
                name="Concrete",
                resource_type="material",
                standard_rate=40,
                cost_per_use=5,
                material_label="m3",
            ),
            CreateResource(client_ref="cost", name="Travel", resource_type="cost"),
            CreateAssignment(
                client_ref="concrete-quantity",
                task=ObjectRef(kind=ObjectKind.TASK, unique_id=22),
                resource=ObjectRef(kind=ObjectKind.RESOURCE, client_ref="material"),
                material_units=80,
            ),
            CreateAssignment(
                client_ref="travel-cost",
                task=ObjectRef(kind=ObjectKind.TASK, unique_id=22),
                resource=ObjectRef(kind=ObjectKind.RESOURCE, client_ref="cost"),
                cost=750,
            ),
        )
        receipt = backend.apply_operations(
            session.project,
            operations,
            idempotency_key="resource-types-0001",
            verification=VerificationLevel.NATIVE_REREAD,
            expected_state=session.state,
        )
        self.assertTrue(all(item["verified"] for item in receipt.observed))
        resources = backend.query(
            session.project, QueryEntity.RESOURCE, fields=(), limit=100, offset=0
        ).items
        material = next(item for item in resources if item["name"] == "Concrete")
        cost = next(item for item in resources if item["name"] == "Travel")
        self.assertEqual(material["standard_rate"], 40)
        self.assertEqual(material["standard_rate_basis"], "material_unit")
        self.assertEqual(material["cost_per_use"], 5)
        self.assertEqual(material["material_label"], "m3")
        self.assertIsNone(material["overtime_rate_per_hour"])
        self.assertIsNone(cost["standard_rate"])
        assignment_items = backend.query(
            session.project, QueryEntity.ASSIGNMENT, fields=(), limit=100, offset=0
        ).items
        material_assignment = next(
            item for item in assignment_items if item["material_units"] == 80
        )
        self.assertIsNone(material_assignment["units_percent"])
        assignment = next(
            item
            for item in assignment_items
            if item["cost"] == 750
        )
        self.assertEqual(assignment["cost"], 750)
        self._discard_and_shutdown(backend, session.project)

    def test_rate_parser_handles_project_formatted_variants(self) -> None:
        self.assertEqual(LiveProjectBackend._rate_number("$1,234.50/h"), 1234.5)
        self.assertEqual(LiveProjectBackend._rate_number("1.234,50 €/Std."), 1234.5)

    def test_baseline_fallback_treats_na_as_unset_and_verifies_set_clear(self) -> None:
        backend, _, _ = self._backend(omit_explicit_baselines=True)
        session = backend.create_project(name="Baseline Fallback", path=None)
        initial = backend.query(
            session.project, QueryEntity.BASELINE, fields=(), limit=20, offset=0
        ).items
        self.assertFalse(any(item["set"] for item in initial))
        set_receipt = backend.apply_operations(
            session.project,
            (SetBaseline(baseline=2),),
            idempotency_key="baseline-fallback-set-0001",
            verification=VerificationLevel.NATIVE_REREAD,
            expected_state=session.state,
        )
        after_set = backend.query(
            session.project, QueryEntity.BASELINE, fields=(), limit=20, offset=0
        ).items
        self.assertTrue(next(item for item in after_set if item["baseline"] == 2)["set"])
        backend.apply_operations(
            session.project,
            (ClearBaseline(baseline=2),),
            idempotency_key="baseline-fallback-clear-0001",
            verification=VerificationLevel.NATIVE_REREAD,
            expected_state=set_receipt.state_after,
        )
        after_clear = backend.query(
            session.project, QueryEntity.BASELINE, fields=(), limit=20, offset=0
        ).items
        self.assertFalse(next(item for item in after_clear if item["baseline"] == 2)["set"])
        self._discard_and_shutdown(backend, session.project)

    def test_live_backend_rechecks_stale_state_before_opening_undo(self) -> None:
        backend, factory, host = self._backend()
        session = backend.create_project(name="TOCTOU", path=None)
        host.call(lambda: factory.server_app.ActiveProject.simulate_task_edit(11, "UI edit"))
        with self.assertRaises(MspError) as stale:
            backend.apply_operations(
                session.project,
                (UpdateTask(task=ObjectRef(kind=ObjectKind.TASK, unique_id=11), name="MCP edit"),),
                idempotency_key="live-stale-0001",
                verification=VerificationLevel.NATIVE_REREAD,
                expected_state=session.state,
            )
        self.assertEqual(stale.exception.code, ErrorCode.STALE_STATE)
        self.assertEqual(factory.undo_open_count, 0)
        task = backend.query(session.project, QueryEntity.TASK, fields=("name",), limit=1, offset=0).items[0]
        self.assertEqual(task["name"], "UI edit")
        self._discard_and_shutdown(backend, session.project)

    def test_calendar_create_rename_and_delete_use_documented_base_calendar_calls(self) -> None:
        backend, factory, _ = self._backend()
        session = backend.create_project(name="Calendars", path=None)
        created = backend.apply_operations(
            session.project,
            (CreateCalendar(client_ref="calendar", name="Build Calendar"),),
            idempotency_key="calendar-create-0001",
            verification=VerificationLevel.NATIVE_REREAD,
            expected_state=session.state,
        )
        calendars = backend.query(session.project, QueryEntity.CALENDAR, fields=(), limit=100, offset=0).items
        created_row = next(item for item in calendars if item["name"] == "Build Calendar")
        ref = ObjectRef(kind=ObjectKind.CALENDAR, guid=created_row["ref"]["guid"])
        renamed = backend.apply_operations(
            session.project,
            (UpdateCalendar(calendar=ref, name="Delivery Calendar"),),
            idempotency_key="calendar-update-0001",
            verification=VerificationLevel.NATIVE_REREAD,
            expected_state=created.state_after,
        )
        self.assertIn(
            "Delivery Calendar",
            [item["name"] for item in backend.query(session.project, QueryEntity.CALENDAR, fields=(), limit=100, offset=0).items],
        )
        deleted = backend.apply_operations(
            session.project,
            (DeleteCalendar(calendar=ref),),
            idempotency_key="calendar-delete-0001",
            verification=VerificationLevel.NATIVE_REREAD,
            expected_state=renamed.state_after,
        )
        self.assertNotEqual(deleted.state_before, deleted.state_after)
        self.assertEqual(factory.calculate_count, 3)
        self._discard_and_shutdown(backend, session.project)

    def test_task_creation_uses_before_and_object_outline_indent_for_wbs_placement(self) -> None:
        backend, factory, _ = self._backend()
        session = backend.create_project(name="WBS", path=None)
        receipt = backend.apply_operations(
            session.project,
            (
                CreateTask(
                    client_ref="last-child",
                    name="Last child",
                    parent=ObjectRef(kind=ObjectKind.TASK, unique_id=11),
                ),
                CreateTask(
                    client_ref="after-existing",
                    name="After existing",
                    parent=ObjectRef(kind=ObjectKind.TASK, unique_id=11),
                    after=ObjectRef(kind=ObjectKind.TASK, unique_id=22),
                ),
                CreateTask(
                    client_ref="inherited-parent",
                    name="Inherited parent",
                    after=ObjectRef(kind=ObjectKind.TASK, client_ref="after-existing"),
                ),
            ),
            idempotency_key="live-wbs-placement-0001",
            verification=VerificationLevel.NATIVE_REREAD,
            expected_state=session.state,
        )
        self.assertTrue(all(item["verified"] for item in receipt.observed))
        order_and_parents = backend._sta.call(
            lambda: [
                (
                    task.UniqueID,
                    task.OutlineParent.UniqueID if task.OutlineParent is not None else None,
                )
                for task in factory.server_app.ActiveProject.Tasks
            ]
        )
        self.assertEqual(order_and_parents, [(11, None), (22, 11), (24, 11), (25, 11), (23, 11)])
        self.assertEqual(factory.task_add_calls, [("Last child", None), ("After existing", 3), ("Inherited parent", 4)])
        self.assertEqual(factory.outline_indent_calls, [23])
        self.assertEqual(factory.selection_api_calls, [])
        self.assertEqual(factory.undo_open_count, 1)
        self.assertEqual(factory.calculate_count, 1)
        self._discard_and_shutdown(backend, session.project)

    def test_invalid_task_placement_and_move_reject_before_mutation(self) -> None:
        backend, factory, _ = self._backend()
        session = backend.create_project(name="Invalid WBS", path=None)
        before = backend.current_state(session.project)
        with self.assertRaises(MspError) as invalid:
            backend.apply_operations(
                session.project,
                (
                    CreateTask(client_ref="root-sibling", name="Root sibling"),
                    CreateTask(
                        client_ref="invalid-child",
                        name="Invalid child",
                        parent=ObjectRef(kind=ObjectKind.TASK, unique_id=11),
                        after=ObjectRef(kind=ObjectKind.TASK, client_ref="root-sibling"),
                    ),
                ),
                idempotency_key="live-invalid-placement-0001",
                verification=VerificationLevel.NATIVE_REREAD,
                expected_state=before,
            )
        self.assertEqual(invalid.exception.code, ErrorCode.INVALID_REQUEST)
        with self.assertRaises(MspError) as unsupported:
            backend.apply_operations(
                session.project,
                (MoveTask(task=ObjectRef(kind=ObjectKind.TASK, unique_id=22), to_root=True),),
                idempotency_key="live-move-unsupported-0001",
                verification=VerificationLevel.NATIVE_REREAD,
                expected_state=before,
            )
        self.assertEqual(unsupported.exception.code, ErrorCode.UNSUPPORTED_OPERATION)
        self.assertEqual(factory.undo_open_count, 0)
        self.assertEqual(factory.task_add_calls, [])
        self.assertEqual(backend.current_state(session.project), before)
        self._discard_and_shutdown(backend, session.project)

    def test_task_placement_reread_catches_wrong_parent_and_row_order(self) -> None:
        backend, factory, _ = self._backend()
        session = backend.create_project(name="WBS verification", path=None)
        operation = CreateTask(
            client_ref="verify-parent",
            name="Verify parent",
            parent=ObjectRef(kind=ObjectKind.TASK, unique_id=11),
        )
        factory.force_created_parent_mismatch = True
        with self.assertRaises(MspError) as wrong_parent:
            backend.apply_operations(
                session.project,
                (operation,),
                idempotency_key="wbs-wrong-parent-0001",
                verification=VerificationLevel.NATIVE_REREAD,
                expected_state=session.state,
            )
        self.assertEqual(wrong_parent.exception.code, ErrorCode.VERIFICATION_FAILED)
        self.assertEqual(backend.current_state(session.project), session.state)

        factory.force_created_parent_mismatch = False
        boundary = backend.apply_operations(
            session.project,
            (
                CreateTask(
                    client_ref="boundary",
                    name="Boundary",
                    parent=ObjectRef(kind=ObjectKind.TASK, unique_id=11),
                ),
            ),
            idempotency_key="wbs-boundary-0001",
            verification=VerificationLevel.NATIVE_REREAD,
            expected_state=session.state,
        )
        factory.force_created_row_mismatch = True
        with self.assertRaises(MspError) as wrong_order:
            backend.apply_operations(
                session.project,
                (
                    CreateTask(
                        client_ref="wrong-order",
                        name="Wrong order",
                        parent=ObjectRef(kind=ObjectKind.TASK, unique_id=11),
                        after=ObjectRef(kind=ObjectKind.TASK, unique_id=22),
                    ),
                ),
                idempotency_key="wbs-wrong-order-0001",
                verification=VerificationLevel.NATIVE_REREAD,
                expected_state=boundary.state_after,
            )
        self.assertEqual(wrong_order.exception.code, ErrorCode.VERIFICATION_FAILED)
        self.assertEqual(backend.current_state(session.project), boundary.state_after)
        factory.force_created_row_mismatch = False
        self._discard_and_shutdown(backend, session.project)

    def test_write_failure_rolls_back_or_reports_uncertain_state(self) -> None:
        backend, factory, _ = self._backend()
        session = backend.create_project(name="Rollback", path=None)
        factory.fail_calculate = True
        with self.assertRaises(MspError) as restored:
            backend.apply_operations(
                session.project,
                (UpdateTask(task=ObjectRef(kind=ObjectKind.TASK, unique_id=11), name="Temporary"),),
                idempotency_key="live-rollback-0001",
                verification=VerificationLevel.NATIVE_REREAD,
                expected_state=session.state,
            )
        self.assertEqual(restored.exception.code, ErrorCode.WRITE_ROLLED_BACK)
        self.assertEqual(backend.current_state(session.project), session.state)
        factory.fail_calculate = False
        factory.fail_undo = True
        expected = backend.current_state(session.project)
        factory.fail_calculate = True
        with self.assertRaises(BackendExecutionError) as uncertain:
            backend.apply_operations(
                session.project,
                (UpdateTask(task=ObjectRef(kind=ObjectKind.TASK, unique_id=11), name="Uncertain"),),
                idempotency_key="live-rollback-0002",
                verification=VerificationLevel.NATIVE_REREAD,
                expected_state=expected,
            )
        self.assertEqual(uncertain.exception.dispatch_state, DispatchState.MAY_HAVE_DISPATCHED)
        factory.fail_calculate = False
        factory.fail_undo = False
        self._discard_and_shutdown(backend, session.project)

    def test_reread_mismatch_is_rolled_back_with_stable_error(self) -> None:
        backend, factory, _ = self._backend()
        session = backend.create_project(name="Verify", path=None)
        factory.force_reread_mismatch = True
        with self.assertRaises(MspError) as mismatch:
            backend.apply_operations(
                session.project,
                (UpdateTask(task=ObjectRef(kind=ObjectKind.TASK, unique_id=11), duration_minutes=300),),
                idempotency_key="live-verify-0001",
                verification=VerificationLevel.NATIVE_REREAD,
                expected_state=session.state,
            )
        self.assertEqual(mismatch.exception.code, ErrorCode.VERIFICATION_FAILED)
        self.assertEqual(backend.current_state(session.project), session.state)
        factory.force_reread_mismatch = False
        self._discard_and_shutdown(backend, session.project)

    def test_false_calculate_result_rolls_back_transactional_writes(self) -> None:
        backend, factory, _ = self._backend()
        session = backend.create_project(name="False Calculate", path=None)
        factory.calculate_result = False
        with self.assertRaises(MspError) as failure:
            backend.apply_operations(
                session.project,
                (UpdateTask(task=ObjectRef(kind=ObjectKind.TASK, unique_id=11), name="Changed"),),
                idempotency_key="false-calculate-0001",
                verification=VerificationLevel.NATIVE_REREAD,
                expected_state=session.state,
            )
        self.assertEqual(failure.exception.code, ErrorCode.VERIFICATION_FAILED)
        self.assertEqual(factory.undo_count, 1)
        self.assertEqual(backend.current_state(session.project), session.state)
        factory.calculate_result = True
        self._discard_and_shutdown(backend, session.project)

    def test_false_schedule_result_has_unknown_dispatch_state(self) -> None:
        backend, factory, _ = self._backend()
        session = backend.create_project(name="False Level", path=None)
        factory.level_result = False
        with self.assertRaises(BackendExecutionError) as failure:
            backend.schedule(
                session.project,
                ScheduleCommand.LEVEL,
                ScheduleOptions(),
                expected_state=session.state,
            )
        self.assertEqual(failure.exception.dispatch_state, DispatchState.MAY_HAVE_DISPATCHED)
        factory.level_result = True
        self._discard_and_shutdown(backend, session.project)

    def test_application_scoped_calls_reactivate_requested_project(self) -> None:
        backend, factory, host = self._backend()
        first = backend.create_project(name="First", path=None)
        second = backend.create_project(name="Second", path=None)
        first_guid = host.call(
            lambda: backend._sessions[first.project.session_id].native_guid
        )
        marker = len(factory.application_scopes)
        changed = backend.apply_operations(
            first.project,
            (UpdateTask(task=ObjectRef(kind=ObjectKind.TASK, unique_id=11), name="First changed"),),
            idempotency_key="multi-project-apply-0001",
            verification=VerificationLevel.NATIVE_REREAD,
            expected_state=first.state,
        )
        backend.schedule(
            first.project,
            ScheduleCommand.CALCULATE,
            ScheduleOptions(),
            expected_state=changed.state_after,
        )
        backend.update_status(
            first.project,
            (TaskProgressUpdate(task=ObjectRef(kind=ObjectKind.TASK, unique_id=22), percent_complete=10),),
            expected_state=backend.current_state(first.project),
        )
        scoped = factory.application_scopes[marker:]
        self.assertTrue(scoped)
        self.assertTrue(all(guid == first_guid for _, guid in scoped), scoped)
        backend.close_project(
            first.project,
            CloseDisposition.DISCARD_AND_CLOSE,
            expected_state=backend.current_state(first.project),
        )
        backend.close_project(
            second.project,
            CloseDisposition.DISCARD_AND_CLOSE,
            expected_state=backend.current_state(second.project),
        )
        backend.shutdown()

    def test_status_schedule_analysis_and_export_use_native_surfaces(self) -> None:
        backend, factory, _ = self._backend()
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source.mpp")
            with open(source, "wb") as stream:
                stream.write(b"fake-mpp")
            session = backend.open_project(path=source)
            updated = backend.update_status(
                session.project,
                (TaskProgressUpdate(task=ObjectRef(kind=ObjectKind.TASK, unique_id=11), percent_complete=50),),
                expected_state=session.state,
            )
            self.assertEqual(updated["updated"], 1)
            expected = backend.current_state(session.project)
            scheduled = backend.schedule(
                session.project,
                ScheduleCommand.RESCHEDULE,
                ScheduleOptions(reschedule_uncompleted_work_to=datetime(2027, 2, 1, 8, 0)),
                expected_state=expected,
            )
            self.assertEqual(scheduled["command"], "reschedule")
            self.assertEqual(factory.schedule_calls[-1][-1], 2)
            analysis = backend.analyze(session.project, AnalysisKind.CRITICAL_PATH, None)
            self.assertTrue(analysis["items"])
            with self.assertRaises(MspError) as unsupported_baseline:
                backend.analyze(session.project, AnalysisKind.VARIANCE, 1)
            self.assertEqual(unsupported_baseline.exception.code, ErrorCode.UNSUPPORTED_OPERATION)
            before_level = backend.current_state(session.project)
            backend.schedule(
                session.project,
                ScheduleCommand.LEVEL,
                ScheduleOptions(clear_existing_leveling=True),
                expected_state=before_level,
            )
            self.assertEqual(factory.schedule_calls[-2:], [("clear", True), ("level", True)])
            pdf = os.path.join(directory, "plan.pdf")
            pdf_result = backend.export(
                session.project,
                "pdf",
                pdf,
                ExportOptions(),
                expected_state=backend.current_state(session.project),
            )
            self.assertGreater(pdf_result["size_bytes"], 0)
            backend.save_project(
                session.project,
                path=source,
                expected_state=backend.current_state(session.project),
            )
            copied = os.path.join(directory, "copy.mpp")
            mpp_result = backend.export(
                session.project,
                "mpp",
                copied,
                ExportOptions(),
                expected_state=backend.current_state(session.project),
            )
            self.assertGreater(mpp_result["size_bytes"], 0)
            with self.assertRaises(MspError):
                backend.export(
                    session.project,
                    "pdf",
                    pdf,
                    ExportOptions(overwrite=False),
                    expected_state=backend.current_state(session.project),
                )
            backend.close_project(
                session.project,
                CloseDisposition.REFUSE_IF_DIRTY,
                expected_state=backend.current_state(session.project),
            )
        backend.shutdown()

    def test_busy_rejected_call_retries_only_before_dispatch_and_preserves_globals(self) -> None:
        backend, factory, _ = self._backend()
        session = backend.create_project(name="Busy", path=None)
        factory.busy_level_failures = 2
        result = backend.schedule(
            session.project,
            ScheduleCommand.LEVEL,
            ScheduleOptions(),
            expected_state=session.state,
        )
        self.assertEqual(result["command"], "level")
        self.assertEqual(factory.level_attempts, 3)
        self.assertEqual(factory.global_option, "unchanged")
        self._discard_and_shutdown(backend, session.project)

    def test_daily_timephased_actual_work_uses_documented_constants_and_rereads(self) -> None:
        backend, factory, host = self._backend()
        session = backend.create_project(name="Timephased", path=None)
        day = datetime(2027, 1, 5, 8, 0)
        result = backend.update_status(
            session.project,
            (
                TimephasedWorkUpdate(
                    assignment=ObjectRef(kind=ObjectKind.ASSIGNMENT, unique_id=41),
                    date=day,
                    actual_work_minutes=180,
                ),
            ),
            expected_state=session.state,
        )
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["observed"][0]["native"]["actual_work_minutes"], 180)
        self.assertEqual(
            factory.timephased_calls,
            [(41, day, day, 10, 4, 1), (41, day, day, 10, 4, 1)],
        )
        self.assertEqual(factory.timescale_item_calls, [1, 1])
        assignment = backend.query(
            session.project,
            QueryEntity.ASSIGNMENT,
            fields=("actual_work_minutes",),
            limit=10,
            offset=0,
        ).items[0]
        self.assertEqual(assignment["actual_work_minutes"], 180)
        self.assertEqual(factory.calculate_count, 1)
        self.assertEqual(factory.selection_api_calls, [])
        self.assertEqual(set(factory.access_threads), {host.owner_thread_id})
        self._discard_and_shutdown(backend, session.project)

    def test_timephased_duplicates_reject_before_undo_or_mutation(self) -> None:
        backend, factory, _ = self._backend()
        session = backend.create_project(name="Duplicate actuals", path=None)
        assignment = ObjectRef(kind=ObjectKind.ASSIGNMENT, unique_id=41)
        with self.assertRaises(MspError) as duplicate:
            backend.update_status(
                session.project,
                (
                    TimephasedWorkUpdate(
                        assignment=assignment,
                        date=datetime(2027, 1, 5, 8, 0),
                        actual_work_minutes=60,
                    ),
                    TimephasedWorkUpdate(
                        assignment=assignment,
                        date=datetime(2027, 1, 5, 17, 0),
                        actual_work_minutes=120,
                    ),
                ),
                expected_state=session.state,
            )
        self.assertEqual(duplicate.exception.code, ErrorCode.INVALID_REQUEST)
        self.assertEqual(factory.undo_open_count, 0)
        self.assertEqual(factory.timephased_calls, [])
        self.assertEqual(backend.current_state(session.project), session.state)
        self._discard_and_shutdown(backend, session.project)

    def test_timephased_reread_mismatch_rolls_back_or_reports_uncertain(self) -> None:
        backend, factory, _ = self._backend()
        session = backend.create_project(name="Actual work rollback", path=None)
        update = TimephasedWorkUpdate(
            assignment=ObjectRef(kind=ObjectKind.ASSIGNMENT, unique_id=41),
            date=datetime(2027, 1, 6, 8, 0),
            actual_work_minutes=90,
        )
        factory.force_timephased_reread_mismatch = True
        with self.assertRaises(MspError) as restored:
            backend.update_status(session.project, (update,), expected_state=session.state)
        self.assertEqual(restored.exception.code, ErrorCode.VERIFICATION_FAILED)
        self.assertEqual(backend.current_state(session.project), session.state)
        factory.fail_undo = True
        with self.assertRaises(BackendExecutionError) as uncertain:
            backend.update_status(session.project, (update,), expected_state=session.state)
        self.assertEqual(uncertain.exception.dispatch_state, DispatchState.MAY_HAVE_DISPATCHED)
        factory.force_timephased_reread_mismatch = False
        factory.fail_undo = False
        self._discard_and_shutdown(backend, session.project)

    def test_attach_is_detach_only_and_shutdown_never_closes_or_quits_user_app(self) -> None:
        backend, factory, _ = self._backend()
        server_owned = backend.create_project(name="Server Plan", path=None)
        backend.save_project(
            server_owned.project,
            path=os.path.join(tempfile.gettempdir(), "fake-server.mpp"),
            expected_state=server_owned.state,
        )
        attached = backend.attach_project(name="User Plan")
        self.assertEqual(attached.ownership, Ownership.ATTACHED_USER_OWNED)
        with self.assertRaises(MspError) as close:
            backend.close_project(
                attached.project, CloseDisposition.SAVE_AND_CLOSE, expected_state=attached.state
            )
        self.assertEqual(close.exception.code, ErrorCode.OWNERSHIP_VIOLATION)
        saved_attached = backend.save_project(attached.project, path=None, expected_state=attached.state)
        self.assertFalse(saved_attached.dirty)
        backend.detach_project(attached.project)
        backend.shutdown()
        self.assertEqual(factory.server_quit_count, 1)
        self.assertEqual(factory.user_quit_count, 0)
        self.assertIn("server", factory.closed_documents)
        self.assertNotIn("user", factory.closed_documents)

    def test_dirty_server_project_requires_explicit_disposition_and_blocks_shutdown(self) -> None:
        backend, factory, _ = self._backend()
        session = backend.create_project(name="Dirty", path=None)
        with self.assertRaises(MspError) as close:
            backend.close_project(
                session.project, CloseDisposition.REFUSE_IF_DIRTY, expected_state=session.state
            )
        self.assertEqual(close.exception.code, ErrorCode.INVALID_REQUEST)
        with self.assertRaises(MspError) as shutdown:
            backend.shutdown()
        self.assertEqual(shutdown.exception.code, ErrorCode.INVALID_REQUEST)
        self.assertEqual(factory.server_quit_count, 0)
        backend.save_project(
            session.project,
            path=os.path.join(tempfile.gettempdir(), "fake-dirty.mpp"),
            expected_state=session.state,
        )
        backend.shutdown()
        self.assertEqual(factory.server_quit_count, 1)

    def test_untitled_save_and_save_close_reject_before_modal_prompt(self) -> None:
        backend, factory, _ = self._backend()
        session = backend.create_project(name="Untitled", path=None)
        with self.assertRaises(MspError) as save_error:
            backend.save_project(session.project, path=None, expected_state=session.state)
        self.assertEqual(save_error.exception.code, ErrorCode.INVALID_REQUEST)
        with self.assertRaises(MspError) as close_error:
            backend.close_project(
                session.project,
                CloseDisposition.SAVE_AND_CLOSE,
                expected_state=session.state,
            )
        self.assertEqual(close_error.exception.code, ErrorCode.INVALID_REQUEST)
        self.assertFalse(factory.modal_prompt_attempted)
        self.assertEqual(factory.file_close_types, [])
        self._discard_and_shutdown(backend, session.project)

    def test_false_save_and_close_results_never_commit_or_drop_session(self) -> None:
        backend, factory, _ = self._backend()
        session = backend.create_project(name="False results", path=None)
        target = os.path.join(tempfile.gettempdir(), "fake-false-save.mpp")
        factory.save_as_result = False
        with self.assertRaises(MspError):
            backend.save_project(session.project, path=target, expected_state=session.state)
        self.assertTrue(backend.get_session(session.project).dirty)
        factory.save_as_result = True
        saved = backend.save_project(session.project, path=target, expected_state=session.state)
        factory.file_close_result = False
        with self.assertRaises(MspError):
            backend.close_project(
                session.project,
                CloseDisposition.REFUSE_IF_DIRTY,
                expected_state=saved.state,
            )
        self.assertEqual(backend.get_session(session.project).project, session.project)
        factory.file_close_result = True
        backend.close_project(
            session.project,
            CloseDisposition.REFUSE_IF_DIRTY,
            expected_state=saved.state,
        )
        backend.shutdown()

    def test_shutdown_refusal_retains_clean_server_session(self) -> None:
        backend, factory, _ = self._backend()
        session = backend.create_project(name="Shutdown refusal", path=None)
        target = os.path.join(tempfile.gettempdir(), "fake-shutdown-refusal.mpp")
        saved = backend.save_project(session.project, path=target, expected_state=session.state)
        factory.file_close_result = False
        with self.assertRaises(MspError):
            backend.shutdown()
        self.assertEqual(backend.get_session(session.project).state, saved.state)
        self.assertEqual(factory.server_quit_count, 0)
        factory.file_close_result = True
        backend.shutdown()
        self.assertEqual(factory.server_quit_count, 1)

    def test_failed_create_with_failed_cleanup_is_uncertain_and_session_is_retained(self) -> None:
        backend, factory, host = self._backend()
        factory.save_as_result = False
        factory.file_close_result = False
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "failed-create.mpp")
            with self.assertRaises(BackendExecutionError) as failure:
                backend.create_project(name="Failed create", path=target)
        self.assertEqual(failure.exception.dispatch_state, DispatchState.MAY_HAVE_DISPATCHED)
        retained = host.call(lambda: next(iter(backend._sessions.values())).ref)
        factory.file_close_result = True
        backend.close_project(
            retained,
            CloseDisposition.DISCARD_AND_CLOSE,
            expected_state=backend.current_state(retained),
        )
        backend.shutdown()

    def test_project_closed_and_identity_changed_are_distinct(self) -> None:
        backend, factory, host = self._backend()
        attached = backend.attach_project(name="User Plan")
        host.call(lambda: factory.user_app.ActiveProject.simulate_close())
        with self.assertRaises(MspError) as closed:
            backend.current_state(attached.project)
        self.assertEqual(closed.exception.code, ErrorCode.PROJECT_CLOSED)
        backend.shutdown()

        backend, factory, host = self._backend()
        with tempfile.TemporaryDirectory() as directory:
            original_path = os.path.join(directory, "Original.mpp")
            with open(original_path, "wb") as stream:
                stream.write(b"fake-mpp")
            opened = backend.open_project(path=original_path)
            original = backend.current_state(opened.project)
            host.call(lambda: setattr(factory.server_app.ActiveProject, "UniqueID", 12345))
            self.assertEqual(backend.current_state(opened.project), original)
            different_path = os.path.join(directory, "Different.mpp")
            host.call(
                lambda: factory.server_app.ActiveProject.simulate_full_name_change(different_path)
            )
            with self.assertRaises(MspError) as changed:
                backend.current_state(opened.project)
            self.assertEqual(changed.exception.code, ErrorCode.PROJECT_IDENTITY_CHANGED)
            host.call(
                lambda: factory.server_app.ActiveProject.simulate_full_name_change(original_path)
            )
            backend.shutdown()

    def test_factory_selects_live_only_for_ready_detection(self) -> None:
        ready = _ready_detection()
        selected = object()
        backend = create_backend(
            {"MSP_MCP_BACKEND": "auto"},
            detection=ready,
            live_backend_factory=lambda detection: selected,
        )
        self.assertIs(backend, selected)
        explicit = create_backend(
            {"MSP_MCP_BACKEND": "live"},
            detection=ready,
            live_backend_factory=lambda detection: selected,
        )
        self.assertIs(explicit, selected)
        not_ready = ready.model_copy(update={"com_registered": False})
        self.assertIsInstance(
            create_backend({"MSP_MCP_BACKEND": "auto"}, detection=not_ready),
            UnavailableProjectBackend,
        )
        self.assertIsInstance(
            create_backend({"MSP_MCP_BACKEND": "live"}, detection=not_ready),
            UnavailableProjectBackend,
        )

    def test_sta_start_failure_maps_to_not_dispatched_backend_error(self) -> None:
        def fail_runtime():
            raise RuntimeError("pythoncom unavailable")

        backend = LiveProjectBackend(
            detection=_ready_detection(),
            sta_host=StaHost(runtime_factory=fail_runtime),
            automation_factory_provider=lambda: _FakeAutomationFactory(),
        )
        with self.assertRaises(BackendExecutionError) as raised:
            backend.create_project(name="No dispatch", path=None)
        self.assertEqual(raised.exception.dispatch_state, DispatchState.NOT_DISPATCHED)
        self.assertEqual(raised.exception.details["cause"], "StaWorkerFailedError")

    def test_importing_live_module_does_not_import_com_or_start_thread(self) -> None:
        import ms_project_mcp.live as live_module

        names = ("pythoncom", "win32com", "win32com.client")
        before_modules = {name: name in sys.modules for name in names}
        before_threads = {thread.ident for thread in threading.enumerate()}
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "pythoncom" or name.startswith("win32com"):
                raise AssertionError("live module import loaded the COM runtime")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded_import):
            importlib.reload(live_module)
        after_modules = {name: name in sys.modules for name in names}
        after_threads = {thread.ident for thread in threading.enumerate()}
        self.assertEqual(after_modules, before_modules)
        self.assertEqual(after_threads, before_threads)


class _NoopRuntime:
    coinit_apartmentthreaded = 0x2
    coinit_disable_ole1dde = 0x4

    def initialize(self, flags: int) -> None:
        self.flags = flags

    def pump_waiting_messages(self) -> None:
        return None

    def uninitialize(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
