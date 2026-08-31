"""brw-16 (2026-08-25): the browser provider is neither registered nor required
by configuration.

`test_conversation.py` pins the REGISTRY half of that claim. This file pins the
CONFIGURATION half and what falls out of it:

1. a config with no `[browser]` section at all loads;
2. a config that still HAS one loads, unchanged, and is simply not consulted —
   an operator upgrading mid-flight must not have to edit a file first;
2b. `restart_command` in particular: still accepted, still shape-checked, still
   stored verbatim (including a value naming the retired shell helper), and the
   example the shape check prints no longer points at a module that is going
   away with the browser package (brw-19c, 2026-08-31);
3. a `[conversation]` section still naming the retired provider is handled
   EXPLICITLY, never silently;
4. `autoloop/tests/conftest.py` no longer imports `autoloop`, which is what
   makes `validation.select_validation_commands` able to narrow anything at all.

The direction of §2 is worth stating once, because every test under it looks
like a test of something inert: what is being defended is that the retirement
took no KEY with it. `load_config` is strict by design, so a key removed
becomes `unknown keys in [browser]` on the next start — and that failure lands
on `status` and `doctor` too, i.e. on exactly the commands an operator would
reach for to fix it.
"""

from pathlib import Path

import pytest

import autoloop.config as config_module
from autoloop.config import (
    RESTART_COMMAND_EXAMPLE,
    RETIRED_BROWSER_PROVIDER,
    RETIRED_RESTART_SCRIPT,
    BrowserConfig,
    ConversationConfig,
    load_config,
)
from autoloop.conversation import available_providers
from autoloop.errors import ConfigError

#: An operator's own restart command. It was
#: `["python3", "-m", "autoloop.browser.chrome_restart"]` until brw-19c
#: (2026-08-31) — the implementation this project shipped, in the package that
#: is being retired. The value here is deliberately something the loop neither
#: ships nor recommends, because that is now the only kind of value this key
#: can honestly hold, and a fixture naming a doomed module would keep the
#: retirement blocked on this file.
LEFTOVER_RESTART_COMMAND = ("/opt/autoloop/restart-chrome", "--profile", "autoloop")

#: Every key `[browser]` accepts, written out, so "an unused section is ignored
#: rather than rejected" is checked against the WHOLE section and not just the
#: one key that used to be required.
FULL_BROWSER_SECTION = "\n".join(
    [
        "[browser]",
        'conversation_url = "https://chatgpt.com/c/left-over"',
        'project_url = "https://chatgpt.com/g/g-p-left-over/project"',
        'cdp_url = "http://127.0.0.1:9222"',
        "attach_oversized_diff = true",
        "composer_timeout_seconds = 30.0",
        "input_sync_timeout_seconds = 30.0",
        "send_ready_timeout_seconds = 30.0",
        "submit_timeout_seconds = 60.0",
        "response_start_timeout_seconds = 120.0",
        "response_timeout_seconds = 900.0",
        "reconcile_timeout_seconds = 30.0",
        "poll_interval_seconds = 2.0",
        "stability_seconds = 3.0",
        "restart_command = ["
        + ", ".join(f'"{token}"' for token in LEFTOVER_RESTART_COMMAND)
        + "]",
        "restart_cooldown_seconds = 120.0",
        "rate_limit_backoff_seconds = 60.0",
        "rate_limit_backoff_max_seconds = 600.0",
    ]
)


def write_config(tmp_path, body=""):
    """A config carrying only what is genuinely REQUIRED, plus `body`."""
    path = tmp_path / "config.toml"
    path.write_text(
        (body + "\n" if body else "")
        + f'[paths]\nworkers_root = "{tmp_path / "workers"}"\n',
        encoding="utf-8",
    )
    return path


# ---- 1. no [browser] section --------------------------------------------------


def test_a_config_with_no_browser_section_loads(tmp_path):
    """THE configuration half of the claim. Before brw-16 this raised
    `browser.conversation_url is required`, which is why a live ChatGPT thread
    URL sat in `config.toml` purely to satisfy a validator for a transport
    nothing selected."""
    config = load_config(write_config(tmp_path))

    assert config.browser == BrowserConfig()
    assert config.browser.conversation_url == ""
    assert config.migration_notices == (), "nothing retired was named"


def test_the_default_provider_is_one_that_is_actually_registered(tmp_path):
    """The fail-open this closes: a config with no `[conversation]` section
    would otherwise LOAD cleanly and then die on the first step, because the
    dataclass default still named the unregistered browser seat."""
    config = load_config(write_config(tmp_path))

    assert config.conversation.provider in available_providers()
    assert ConversationConfig().provider in available_providers()
    assert ConversationConfig().fallback_provider == "", "no failover by default"


def test_an_empty_conversation_url_is_not_a_reason_to_refuse(tmp_path):
    """Explicitly empty, rather than absent — the shape a half-migrated config
    has. It must be as acceptable as the section being gone."""
    config = load_config(write_config(tmp_path, '[browser]\nconversation_url = ""'))

    assert config.browser.conversation_url == ""


# ---- 2. a leftover [browser] section ------------------------------------------


def test_a_config_that_still_has_a_full_browser_section_loads(tmp_path):
    """An unused section is IGNORED, not rejected. Refusing here would mean an
    operator has to edit a config file before the upgraded loop will start,
    which is the one thing this change was not allowed to cost."""
    config = load_config(write_config(tmp_path, FULL_BROWSER_SECTION))

    assert config.browser.conversation_url == "https://chatgpt.com/c/left-over"
    assert config.browser.project_url.endswith("/project")
    assert config.browser.restart_command == LEFTOVER_RESTART_COMMAND
    assert config.browser.attach_oversized_diff is True
    assert config.migration_notices == (), "an unused section is not a retired key"


def test_every_browser_key_survived_the_retirement(tmp_path):
    """The compatibility contract stated as a SET, not as whichever keys the
    section above happens to list.

    brw-19c (2026-08-31) removed the last preflight that graded any of these
    and removed no KEY. Dropping one is the change that breaks an unmigrated
    deployment loudly and at the worst moment — `load_config` is strict, so a
    retired key becomes `unknown keys in [browser]` and EVERY command
    (`status`, `doctor`, the recovery commands) fails on the config the
    operator would use them to fix. Asserted against the dataclass so a field
    deleted without a thought about that fails here rather than in the field."""
    import dataclasses
    import tomllib

    fields = {f.name for f in dataclasses.fields(BrowserConfig)}

    assert fields == {
        "conversation_url",
        "cdp_url",
        "attach_oversized_diff",
        "project_url",
        "composer_timeout_seconds",
        "input_sync_timeout_seconds",
        "send_ready_timeout_seconds",
        "submit_timeout_seconds",
        "response_start_timeout_seconds",
        "response_timeout_seconds",
        "reconcile_timeout_seconds",
        "poll_interval_seconds",
        "stability_seconds",
        "restart_command",
        "restart_cooldown_seconds",
        "rate_limit_backoff_seconds",
        "rate_limit_backoff_max_seconds",
    }
    # And the section written out above really does exercise all of them, so
    # "the whole section loads" keeps meaning what it says rather than meaning
    # "the subset someone remembered to list loads".
    assert set(tomllib.loads(FULL_BROWSER_SECTION)["browser"]) == fields


def test_an_unknown_key_in_that_section_is_still_refused(tmp_path):
    """Ignored is not UNVALIDATED. A section that accepted anything would let a
    typo'd setting read as configured — the property `load_config`'s whole
    strict-by-design docstring rests on, and one this must not trade away for
    compatibility."""
    with pytest.raises(ConfigError, match=r"unknown keys in \[browser\]"):
        load_config(write_config(tmp_path, '[browser]\nconversaton_url = "typo"'))


@pytest.mark.parametrize(
    "value",
    [
        '"restart.sh"',  # a bare string — not a list at all
        "[1, 2]",  # a list whose ELEMENTS are wrong, which the outer
        '["ok", 2]',  # `isinstance(cmd, list)` alone would wave through
    ],
    ids=["not-a-list", "no-strings", "one-non-string"],
)
def test_a_malformed_restart_command_in_that_section_is_still_refused(tmp_path, value):
    """Ignored is not UNCHECKED, on the one `[browser]` key that is still READ
    (`cli._repair_browser` runs it). Both halves of the shape check are
    exercised: a value that is not a list, and lists whose elements are not
    strings — the second is the shape that would otherwise reach
    `subprocess.run` as an argv holding an int."""
    with pytest.raises(ConfigError, match="list of strings"):
        load_config(write_config(tmp_path, f"[browser]\nrestart_command = {value}"))


# ---- 2b. the restart command, after the shipped implementation went away ------


def test_the_pasteable_example_names_no_module_in_this_package(tmp_path):
    """A config error is plausibly the only thing an operator sees — `cli.main`
    prints `error: <exc>` and nothing else — so the line it hands them has to
    be one that could work.

    Until brw-19c (2026-08-31) it was
    `["python3", "-m", "autoloop.browser.chrome_restart"]`, the shipped
    implementation. That package is being retired, so pasting that line would
    produce a `restart_command` that fails with `No module named` at the exact
    moment it is reached — a silent browser, mid-run. The example is a
    PLACEHOLDER now, and this test is what stops a plausible-looking module
    path being reintroduced by someone tidying it up."""
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, "[browser]\nrestart_command = 7"))

    message = str(excinfo.value)
    assert "restart_command = [" in message, "still paste-ready"
    assert "autoloop.browser" not in message
    assert "autoloop." not in message, "no module in this package is named at all"
    # Emptied to `()`, the example would render as `restart_command = []` — a
    # line that still satisfies every assertion above while naming no command
    # at all, and the loop below would then check nothing. The negative checks
    # are the ones with teeth here, so the thing they read has to be non-empty.
    assert RESTART_COMMAND_EXAMPLE, "an empty example makes the loop below vacuous"
    for token in RESTART_COMMAND_EXAMPLE:
        assert token in message


def test_the_retired_replacement_constant_is_gone(tmp_path):
    """`config.RESTART_COMMAND_REPLACEMENT` held the module path above. It was
    never imported anywhere — only quoted — so deleting it breaks nothing, and
    keeping it would have left this package spelling out a command that stops
    existing. Pinned as an ABSENCE because the constant is exactly the kind of
    thing a later round restores by reflex while resurrecting the example."""
    assert not hasattr(config_module, "RESTART_COMMAND_REPLACEMENT")


def test_a_restart_command_still_naming_the_retired_shell_helper_loads(tmp_path):
    """THE retired-key boundary for this key, and the one that must not tighten.

    `load_config` deliberately does not act on `RETIRED_RESTART_SCRIPT` (brw-08,
    2026-08-16): the live `.autoloop/config.toml` is not in this repository, so
    refusing here would make `status`, `doctor` and every recovery command fail
    on an unmigrated deployment — taking away the tooling needed to migrate it.
    brw-19c removed browser CHECKS, not this tolerance, and the distinction is
    the whole task: a value is stored EXACTLY as written, neither refused nor
    rewritten, and no notice is raised for it either."""
    config = load_config(
        write_config(
            tmp_path, f'[browser]\nrestart_command = ["./scripts/{RETIRED_RESTART_SCRIPT}"]'
        )
    )

    assert config.browser.restart_command == (f"./scripts/{RETIRED_RESTART_SCRIPT}",)
    assert config.migration_notices == ()


def test_an_empty_restart_command_is_the_default_and_means_no_auto_restart(tmp_path):
    """The state every config that never configured one is in, including every
    config written since the template stopped shipping a `[browser]` section.
    It has to stay loadable and stay FALSY: `cli._repair_browser` reads
    emptiness as "say so and stop" rather than as "guess which Chrome to
    kill"."""
    assert BrowserConfig().restart_command == ()
    assert not load_config(write_config(tmp_path)).browser.restart_command
    assert not load_config(
        write_config(tmp_path, "[browser]\nrestart_command = []")
    ).browser.restart_command


# ---- 3. a [conversation] section naming the retired provider ------------------


def test_a_config_still_naming_the_retired_provider_loads_and_says_so(tmp_path):
    """Not a hard refusal, for the reason every other retired key here is not:
    the live `.autoloop/config.toml` is not in this repository, so refusing at
    load would break `status`, `doctor` and every recovery command on an
    unmigrated deployment — taking away the tooling needed to fix it."""
    config = load_config(
        write_config(tmp_path, f'[conversation]\nprovider = "{RETIRED_BROWSER_PROVIDER}"')
    )

    # NOT rewritten: there is no neutral provider value to migrate to.
    assert config.conversation.provider == RETIRED_BROWSER_PROVIDER
    [notice] = config.migration_notices
    assert RETIRED_BROWSER_PROVIDER in notice
    assert "RETIRED" in notice
    assert "codex_cli" in notice, "the notice says what to set instead"


def test_a_retired_fallback_is_read_as_no_failover_and_says_so(tmp_path):
    """Neutralised rather than left as written, because nothing validates a
    fallback name until the handover happens: `_handle_quota_exhausted` does not
    consult the registry, so an exhausted allowance would switch the reviewer
    role to an unregistered transport and only then fail to build it. `""` is
    the documented value for "park instead", a path that already works."""
    config = load_config(
        write_config(
            tmp_path,
            "[conversation]\n"
            'provider = "codex_cli"\n'
            f'fallback_provider = "{RETIRED_BROWSER_PROVIDER}"',
        )
    )

    assert config.conversation.provider == "codex_cli"
    assert config.conversation.fallback_provider == ""
    [notice] = config.migration_notices
    assert RETIRED_BROWSER_PROVIDER in notice
    assert "failover disabled" in notice


def test_naming_it_in_both_keys_produces_both_notices(tmp_path):
    config = load_config(
        write_config(
            tmp_path,
            "[conversation]\n"
            f'provider = "{RETIRED_BROWSER_PROVIDER}"\n'
            f'fallback_provider = "{RETIRED_BROWSER_PROVIDER}"',
        )
    )

    assert len(config.migration_notices) == 2
    assert config.conversation.fallback_provider == ""


def test_a_healthy_conversation_section_produces_no_notice(tmp_path):
    """The bound on the two tests above: the migration must fire on the retired
    NAME, never on the key being present."""
    config = load_config(
        write_config(
            tmp_path,
            "[conversation]\n"
            'provider = "codex_app_server"\n'
            'fallback_provider = "codex_cli"',
        )
    )

    assert config.conversation.fallback_provider == "codex_cli"
    assert config.migration_notices == ()


# ---- 3b. there is no live-CDP probe left to reach -----------------------------


def test_the_orchestrator_holds_no_cdp_probe_at_all(tmp_path):
    """The fail-open the removed conftest fixture used to cover, closed at the
    source and then removed at the source.

    Until brw-19b `Orchestrator._attachable_page_targets` dialled
    `browser.cdp_url` — a real Chrome on 127.0.0.1:9222 on a developer machine.
    brw-16 argued it was UNREACHABLE on a default run (`_handle_rate_limited`'s
    browser arm asks `conversation.transport_is_browser_backed` first, and no
    registered provider answers yes), which is why the autouse fixture could
    go; brw-19b removed the method and its `autoloop.browser` import outright,
    so there is no longer a probe to be unreachable.

    Asserted on the CLASS and on the MODULE, not on one instance: an instance
    check passes for a class attribute that was merely shadowed, and the module
    check is what fails if the import creeps back in for some other caller. The
    default run is then driven through the handler anyway, because "the method
    is gone" and "no socket is opened" are different claims and only the second
    one is what the fixture protected.
    """
    from autoloop import orchestrator as orchestrator_module
    from autoloop.errors import RateLimitedError
    from autoloop.orchestrator import Orchestrator
    from autoloop.state import Phase

    from test_orchestrator import build  # noqa: E402 - see conftest sys.path

    assert not hasattr(Orchestrator, "_attachable_page_targets")
    assert not hasattr(orchestrator_module, "attachable_page_targets")

    # The PRODUCTION default, named through the dataclass rather than spelled
    # here, so this test moves with it. `test_orchestrator.build` defaults to a
    # browser-backed fake of its own (its transport-fault tests need one), which
    # is exactly what must be overridden to ask this question.
    orch, _, _, _, _, _, _ = build(tmp_path, provider=ConversationConfig().provider)
    assert orch._config.conversation.provider == "codex_cli"
    assert orch._transport_is_browser_backed() is False

    orch._sleep = lambda seconds: None
    orch.state.phase = Phase.AWAITING.value

    orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    assert orch.state.rate_limit_backoffs == 1, "it still backs off, just blindly"
    assert orch.state.phase == Phase.AWAITING.value


# ---- 4. the test-selection edge this change exists to cut ---------------------


def test_the_tests_conftest_does_not_import_autoloop():
    """The saving, pinned where it can regress.

    pytest applies a conftest to its whole directory tree, so one import here is
    an import every test under `autoloop/tests/` has, and
    `validation.select_validation_commands` reads exactly that graph. While this
    file imported `autoloop.orchestrator` — for one autouse fixture stubbing a
    live-CDP probe — a change to almost any module selected the ENTIRE tree (92
    test files for a change to `autoloop/dashboard.py`, measured 2026-08-25).

    Asserted on the SOURCE rather than on the module object on purpose: a
    string-based `monkeypatch.setattr("autoloop.orchestrator...")` would keep
    the runtime dependency while hiding it from the graph, which is the one
    rewrite that must not be mistaken for a fix.
    """
    source = (Path(__file__).resolve().parent / "conftest.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    # The docstring names the module it used to import; strip it before looking.
    body = code.split('"""')[-1]
    assert "import autoloop" not in body
    assert "from autoloop" not in body
    assert "autoloop." not in body
