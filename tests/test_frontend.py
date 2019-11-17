import pathlib
import shutil
import time
from typing import List

from cincan.frontend import ToolImage


def prepare_work_dir(name: str, with_files: List['str']) -> pathlib.Path:
    src_root = pathlib.Path('samples')
    root = pathlib.Path(name)
    if root.is_dir():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir()
    for f_name in with_files:
        src = src_root / f_name
        shutil.copy(src, root / src.name)
        shutil.copystat(src, root / src.name)
    return root


def test_run_get_string():
    tool = ToolImage(image='cincan/env', rm=False)
    out = tool.run_get_string(['echo', 'Hello'])
    assert out == 'Hello\n'

    out = tool.run_get_string(['echxo', 'Hello'])
    assert 'No such file or directory' in out


def test_magic_file_io():
    tool = ToolImage(image='cincan/env', rm=False)
    work_dir = prepare_work_dir('_test', ['source-a.txt'])
    out = tool.run_get_string(['cat', '_test/source-a.txt'])
    assert out == 'Source A\n'
    assert tool.upload_files == ['_test/source-a.txt']
    assert tool.download_files == []

    work_dir = prepare_work_dir('_test', ['source-b.txt'])
    out = tool.run_get_string(['cat', '_test/source-b.txt'])
    assert out == 'Source B\n'
    assert tool.upload_files == ['_test/source-b.txt']
    assert tool.download_files == []

    work_dir = prepare_work_dir('_test', ['source-a.txt'])
    tool.run(["sh", "-c", '''cat _test/source-a.txt > _test/test_a.txt'''])
    assert tool.upload_files == ['_test/source-a.txt']
    assert tool.download_files == ['_test/test_a.txt']

    time.sleep(1.5)  # make sure file timestamp gets old
    tool.run(["sh", "-c", '''cat _test/source-a.txt > _test/test_a.txt'''])
    assert tool.upload_files == ['_test/source-a.txt', '_test/test_a.txt']
    assert tool.download_files == ['_test/test_a.txt']


def test_many_output_files():
    tool = ToolImage(image='cincan/env', rm=False)
    work_dir = prepare_work_dir('_test', ['ab.zip'])
    tool.run_get_string(['unzip', '-d', '_test', '_test/ab.zip'])
    assert tool.upload_files == ['_test/ab.zip']
    assert tool.download_files == ['_test/source-a.txt', '_test/source-b.txt']


def test_explicit_in_out_files():
    tool = ToolImage(image='cincan/env', rm=False)
    work_dir = prepare_work_dir('_test', ['ab.zip', 'empty.file'])
    tool.input_files = ['_test/ab.zip', '_test/empty.file']
    tool.output_files = ['_test/source-b.txt']

    tool.run_get_string(['unzip', '-d', '_test', '_test/ab.zip'])
    assert tool.upload_files == ['_test/ab.zip', '_test/empty.file']
    assert tool.download_files == ['_test/source-b.txt']
