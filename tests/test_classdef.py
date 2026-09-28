from datetime import date
from pathlib import Path

import pytest

from tkctl_lab import classdef
from tkctl_lab.config import VmConfig

VM = VmConfig()


def flags(name, count, **kw):
    kw = {"expires": None, "node": None, "cores": None, "memory": None} | kw
    return classdef.from_flags(name, count, vm=VM, **kw)


def test_from_flags_numbers_students():
    cd = flags("k8s-101", 3, expires="2026-10-20")
    assert [s.name for s in cd.students] == ["01", "02", "03"]
    assert cd.students[0].memory == 24576 and cd.expires == date(2026, 10, 20)
    assert classdef.vm_name("k8s-101", cd.students[2]) == "lab-k8s-101-03"


def test_from_flags_overrides_vm():
    cd = flags("k8s-101", 1, node="pve-node8", cores=12, memory=32768)
    assert (cd.students[0].cores, cd.students[0].memory, cd.node) == (12, 32768, "pve-node8")


def test_from_flags_rejects_bad_name_and_count():
    with pytest.raises(classdef.ClassDefError) as e:
        flags("K8S 101", 0, expires="20-10-2026")
    msg = str(e.value)
    assert "name" in msg and "count" in msg and "expires" in msg


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "class.toml"
    p.write_text(text)
    return p


def write_tmp(text: str) -> Path:
    import tempfile

    d = Path(tempfile.mkdtemp())
    return write(d, text)


def test_from_file_names_and_overrides(tmp_path):
    p = write(
        tmp_path,
        """
name = "k8s-101"
expires = 2026-10-20
students = ["alice", "bob", "carol"]
[vm]
cores = 8
memory = 16384
[[students_override]]
name = "carol"
memory = 32768
""",
    )
    cd = classdef.from_file(p, vm=VM)
    assert [s.name for s in cd.students] == ["alice", "bob", "carol"]
    # class value for memory, config default for balloon
    assert cd.students[0].memory == 16384 and cd.students[0].balloon == 8192
    assert cd.students[2].memory == 32768 and cd.students[2].cores == 8
    assert classdef.vm_name(cd.name, cd.students[0]) == "lab-k8s-101-alice"


def test_from_file_count(tmp_path):
    cd = classdef.from_file(write(tmp_path, 'name = "x"\ncount = 2\n'), vm=VM)
    assert [s.name for s in cd.students] == ["01", "02"]


def test_from_file_lists_every_problem(tmp_path):
    p = write(
        tmp_path,
        """
name = "k8s-101"
expires = "soon"
count = 2
students = ["alice", "alice", "Bob"]
[vm]
memory = "24G"
[[students_override]]
name = "nobody"
""",
    )
    with pytest.raises(classdef.ClassDefError) as e:
        classdef.from_file(p, vm=VM)
    msg = str(e.value)
    for needle in (
        "students and count",
        "duplicate student alice",
        'student "Bob"',
        "vm.memory must be an integer",
        "expires",
        'override for unknown student "nobody"',
    ):
        assert needle in msg, needle


def test_balloon_must_not_exceed_memory():
    with pytest.raises(classdef.ClassDefError, match=r"balloon .* must not exceed memory"):
        flags("k8s-101", 1, memory=4096)  # config balloon 8192 > 4096
    cd = classdef.from_file(
        write_tmp('name = "x"\ncount = 1\n[vm]\nmemory = 4096\nballoon = 4096\n'), vm=VM
    )
    assert cd.students[0].balloon == 4096


def test_student_error_names_the_student_rule(tmp_path):
    with pytest.raises(classdef.ClassDefError) as e:
        classdef.from_file(write(tmp_path, 'name = "x"\nstudents = ["Bob"]\n'), vm=VM)
    assert "[0-9]{2,3}" in str(e.value)


def test_to_toml_round_trips(tmp_path):
    text = (
        'name = "k8s-101"\nexpires = 2026-10-20\nstudents = ["alice", "bob"]\n'
        '[[students_override]]\nname = "bob"\ncores = 12\n'
    )
    cd = classdef.from_file(write(tmp_path, text), vm=VM)
    again = classdef.from_file(write(tmp_path, classdef.to_toml(cd)), vm=VM)
    assert again == cd
