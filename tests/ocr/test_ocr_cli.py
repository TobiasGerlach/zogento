"""The paste-only command works without credentials or option prompts."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from immich_book_ocr import cli
from immich_book_ocr.immich import ImmichError


@pytest.fixture
def album_run(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("IMMICH_API_KEY", raising=False)
    monkeypatch.delenv("IMMICH_SHARE_PASSWORD", raising=False)
    run = Mock(return_value={"complete": True})
    monkeypatch.setattr(cli, "run_album", run)
    return run


@pytest.mark.parametrize("prefix", ["share", "s"])
def test_direct_share_link_needs_no_flags_or_credentials(album_run, capsys, prefix):
    url = f"http://photos.example/{prefix}/PRIVATE_SHARE"
    assert cli.main([url]) == 0
    args, kwargs = album_run.call_args
    assert args[0] == url
    assert args[1].parent == Path("output")
    assert "PRIVATE_SHARE" not in str(args[1])
    assert kwargs["options"].language == "eng+deu"
    assert kwargs["options"].order == "date"
    assert kwargs["options"].engine == "auto"
    assert kwargs["api_key"] is None and kwargs["password"] is None
    console = capsys.readouterr().out
    assert str((args[1] / "book.txt").resolve()) in console
    assert str((args[1] / "review.html").resolve()) in console
    assert "PRIVATE_SHARE" not in console


@pytest.mark.parametrize("argv", [[], ["album"]])
def test_prompt_asks_for_link_only(album_run, monkeypatch, argv):
    prompt = Mock(return_value="  http://photos.example/share/PRIVATE_SHARE  ")
    monkeypatch.setattr("builtins.input", prompt)
    assert cli.main(argv) == 0
    prompt.assert_called_once_with("Immich share link: ")
    assert album_run.call_args.args[0] == "http://photos.example/share/PRIVATE_SHARE"


def test_repeat_link_reuses_output_and_different_link_is_separate(album_run):
    url = "http://photos.example/share/FIRST"
    cli.main([url])
    first = album_run.call_args.args[1]
    first.mkdir(parents=True)
    saved = first / "saved-original"
    saved.write_bytes(b"downloaded scan")
    cli.main([" " + url + " "])
    assert album_run.call_args.args[1] == first
    assert saved.read_bytes() == b"downloaded scan"
    cli.main(["http://photos.example/share/SECOND"])
    assert album_run.call_args.args[1] != first


def test_explicit_output_and_advanced_options_still_work(album_run):
    assert cli.main([
        "album", "http://photos.example/share/KEY", "--output", "my-book",
        "--language", "fra", "--order", "filename", "--single-page",
    ]) == 0
    args, kwargs = album_run.call_args
    assert args[1] == Path("my-book")
    assert kwargs["options"].language == "fra"
    assert kwargs["options"].order == "filename"
    assert kwargs["options"].single_page is True


def test_local_command_can_choose_output_automatically(album_run, monkeypatch):
    run = Mock(return_value={"complete": True})
    monkeypatch.setattr(cli, "run_local", run)
    assert cli.main(["local", "scans"]) == 0
    args, kwargs = run.call_args
    assert args[0] == Path("scans")
    assert args[1].parent == Path("output")
    assert args[1].name.startswith("local-")
    assert kwargs["options"].language == "eng+deu"
    assert kwargs["options"].order == "date"
    album_run.assert_not_called()


def test_partial_ocr_retains_exit_code_two(album_run):
    album_run.return_value = {"complete": False}
    assert cli.main(["http://photos.example/share/KEY"]) == 2


def test_connection_error_is_reported_without_traceback(album_run, capsys):
    album_run.side_effect = ImmichError("The network connection failed.")
    assert cli.main(["http://photos.example/share/KEY"]) == 1
    assert capsys.readouterr().err == "Error: The network connection failed.\n"


@pytest.mark.parametrize("error,expected", [(EOFError, 1), (KeyboardInterrupt, 130)])
def test_interrupted_input_exits_cleanly(album_run, monkeypatch, error, expected):
    monkeypatch.setattr("builtins.input", Mock(side_effect=error))
    assert cli.main([]) == expected
    album_run.assert_not_called()


def test_empty_link_is_rejected_without_starting_ocr(album_run, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _: "   ")
    assert cli.main([]) == 1
    album_run.assert_not_called()
    assert "Paste an Immich share link" in capsys.readouterr().err
