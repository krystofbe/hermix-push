import stat

from hermex_push.keys import load_or_create_keys


def test_keys_are_created_once_with_private_permissions(hermes_home):
    first = load_or_create_keys()
    second = load_or_create_keys()
    assert first == second
    assert len(first.install_key) == 64 and int(first.install_key, 16)
    assert len(first.preview_key) == 32
    directory = hermes_home / "plugin-data" / "hermix-push"
    for name in ("install_key", "preview_key"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600


def test_profile_homes_share_the_root_keys(hermes_home, monkeypatch):
    root_keys = load_or_create_keys()
    profile = hermes_home / "profiles" / "dev"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    assert load_or_create_keys() == root_keys
    assert not (profile / "plugins").exists()


def test_keys_left_in_the_install_directory_are_migrated(hermes_home):
    legacy = hermes_home / "plugins" / "hermix-push"
    legacy.mkdir(parents=True)
    (legacy / "install_key").write_text("cd" * 32)
    (legacy / "preview_key").write_bytes(bytes(range(32)))
    keys = load_or_create_keys()
    assert keys.install_key == "cd" * 32 and keys.preview_key == bytes(range(32))
    assert not (legacy / "install_key").exists() and not (legacy / "preview_key").exists()
    assert load_or_create_keys() == keys


def test_keys_paired_under_the_old_plugin_name_are_copied_and_left_in_place(hermes_home):
    old = hermes_home / "plugin-data" / "hermex-push"
    old.mkdir(parents=True)
    (old / "install_key").write_text("ef" * 32)
    (old / "preview_key").write_bytes(bytes(range(32)))
    keys = load_or_create_keys()
    assert keys.install_key == "ef" * 32 and keys.preview_key == bytes(range(32))
    assert (old / "install_key").read_text() == "ef" * 32 and (old / "preview_key").exists()
    new = hermes_home / "plugin-data" / "hermix-push"
    assert stat.S_IMODE((new / "install_key").stat().st_mode) == 0o600


def test_old_keys_never_overwrite_keys_the_new_name_already_has(hermes_home):
    first = load_or_create_keys()
    old = hermes_home / "plugin-data" / "hermex-push"
    old.mkdir(parents=True)
    (old / "install_key").write_text("ef" * 32)
    (old / "preview_key").write_bytes(bytes(range(32)))
    assert load_or_create_keys() == first


def test_an_incomplete_old_pair_is_not_copied(hermes_home):
    old = hermes_home / "plugin-data" / "hermex-push"
    old.mkdir(parents=True)
    (old / "install_key").write_text("ef" * 32)
    assert load_or_create_keys().install_key != "ef" * 32
