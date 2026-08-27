from handoff import auth, cli, db

# Must be at least MIN_PASSWORD_LEN (12) or createuser rejects it before doing anything.
GOOD = "correct horse battery staple"
ALSO_GOOD = "a different long passphrase"


def test_createuser_creates_a_user(db_path, monkeypatch):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": GOOD)
    assert cli.main(["--db", db_path, "createuser", "yoshi"]) == 0

    c = db.connect(db_path)
    auth.reset_throttle()
    assert auth.verify_user(c, "yoshi", GOOD) is not None
    c.close()


def test_createuser_rejects_a_mismatched_confirmation(db_path, monkeypatch):
    # Both answers clear the length check, so this exercises the mismatch branch itself
    # rather than short-circuiting on length.
    answers = iter([GOOD, ALSO_GOOD])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    assert cli.main(["--db", db_path, "createuser", "yoshi"]) == 1

    c = db.connect(db_path)
    db.init_schema(c)
    assert cli.user_count(c) == 0
    c.close()


def test_createuser_rejects_a_duplicate_username(db_path, monkeypatch):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": GOOD)
    assert cli.main(["--db", db_path, "createuser", "yoshi"]) == 0
    assert cli.main(["--db", db_path, "createuser", "yoshi"]) == 1


def test_createuser_rejects_a_short_password(db_path, monkeypatch):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "short")
    assert cli.main(["--db", db_path, "createuser", "yoshi"]) == 1


def test_reap_reports_what_it_deleted(db_path, capsys):
    assert cli.main(["--db", db_path, "reap"]) == 0
    assert "0" in capsys.readouterr().out


def test_serve_refuses_with_no_users(db_path, capsys):
    assert cli.main(["--db", db_path, "serve"]) == 1
    assert "createuser" in capsys.readouterr().err


def test_serve_never_defaults_to_all_interfaces():
    parser = cli.build_parser()
    args = parser.parse_args(["serve"])
    assert args.bind == "127.0.0.1"


def test_bind_to_all_interfaces_is_refused(db_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": GOOD)
    assert cli.main(["--db", db_path, "createuser", "yoshi"]) == 0

    assert cli.main(["--db", db_path, "serve", "--bind", "0.0.0.0"]) == 1  # noqa: S104
    assert "0.0.0.0" in capsys.readouterr().err  # noqa: S104


def test_bind_to_all_interfaces_allowed_with_explicit_flag():
    args = cli.build_parser().parse_args(
        ["serve", "--bind", "0.0.0.0", "--allow-any-interface"]  # noqa: S104
    )
    assert args.allow_any_interface is True


def test_logout_all_deletes_every_session_for_the_user(db_path, capsys):
    c = db.connect(db_path)
    db.init_schema(c)
    uid = auth.create_user(c, "yoshi", GOOD)
    sid_a = auth.create_session(c, uid)
    sid_b = auth.create_session(c, uid)
    c.close()

    assert cli.main(["--db", db_path, "logout-all", "yoshi"]) == 0
    assert "2" in capsys.readouterr().out

    c = db.connect(db_path)
    assert auth.session_user(c, sid_a) is None
    assert auth.session_user(c, sid_b) is None
    c.close()


def test_logout_all_rejects_an_unknown_username(db_path, capsys):
    assert cli.main(["--db", db_path, "logout-all", "nobody"]) == 1
    assert "nobody" in capsys.readouterr().err
