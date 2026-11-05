"""The aclip dispatch: every pipeline stage is wired (ticket #29 completes the CLI)."""

WIRED = (
    "doctor",
    "transcript-fetch",
    "compare",
    "transcribe",
    "scaffold",
    "download",
    "render",
)


def test_help_exits_zero_and_lists_every_stage(run_cli) -> None:
    code, out, err = run_cli("--help")
    assert code == 0
    assert "usage: aclip" in out + err
    for name in WIRED:
        assert name in out + err


def test_no_command_prints_help_and_exits_two(run_cli) -> None:
    code, out, err = run_cli()
    assert code == 2
    assert "usage: aclip" in out + err


def test_no_stub_subcommands_remain(run_cli) -> None:
    """The frozen list is fully wired: no stage answers 'not implemented'."""
    for name in WIRED:
        check = run_cli(name, "--help")
        assert check[0] == 0, f"{name}: rc={check[0]} err={check[2]!r}"
        assert "not implemented" not in check[1] + check[2]
