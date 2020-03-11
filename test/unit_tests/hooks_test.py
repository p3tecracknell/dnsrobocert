import datetime
import os

import pytest
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from mock import call, patch

from dnsrobocert.core import config, hooks

try:
    POSIX_MODE = True
    import pwd
    import grp
except ImportError:
    POSIX_MODE = False


LINEAGE = "test.example.com"


@pytest.fixture(autouse=True)
def fake_env(tmp_path, monkeypatch):
    live_path = tmp_path / "live" / LINEAGE
    archive_path = tmp_path / "archive" / LINEAGE
    os.makedirs(str(tmp_path / "live"))
    os.makedirs(str(archive_path))
    os.symlink(str(archive_path), str(live_path), target_is_directory=True)

    monkeypatch.setenv("CERTBOT_VALIDATION", "VALIDATION")
    monkeypatch.setenv("CERTBOT_DOMAIN", LINEAGE)
    monkeypatch.setenv("RENEWED_LINEAGE", str(live_path))

    yield {
        "live": live_path,
        "archive": archive_path,
    }


@pytest.fixture
def fake_config(tmp_path):
    config_path = tmp_path / "config.yml"
    config_data = """
    acme:
      certs_permissions:
        files_mode: 0666
        dirs_mode: 0777
        user: nobody
        group: nogroup
    profiles:
    - name: dummy-profile
      provider: dummy
      provider_options:
        auth_token: TOKEN
      sleep_time: 0.1
    certificates:
    - name: {0}
      domains:
      - {0}
      profile: dummy-profile
      pfx:
        export: true
      autocmd:
      - cmd: echo 'Hello World!'
        containers: [foo, bar]
      autorestart:
      - containers: [container1, container2]
      - swarm_services: [service1, service2]
    """.format(
        LINEAGE
    )
    config_path.write_text(config_data)
    yield config_path


@patch("dnsrobocert.core.hooks.Client")
def test_auth_cli(client, fake_config):
    hooks.main(["-t", "auth", "-c", str(fake_config), "-l", LINEAGE])

    assert len(client.call_args[0]) == 1
    resolver = client.call_args[0][0]

    assert resolver.resolve("lexicon:action") == "create"
    assert resolver.resolve("lexicon:domain") == LINEAGE
    assert resolver.resolve("lexicon:type") == "TXT"
    assert resolver.resolve("lexicon:name") == "_acme-challenge.{0}.".format(LINEAGE)
    assert resolver.resolve("lexicon:content") == "VALIDATION"
    assert resolver.resolve("lexicon:provider_name") == "dummy"
    assert resolver.resolve("lexicon:dummy:auth_token") == "TOKEN"


@patch("dnsrobocert.core.hooks.Client")
def test_cleanup_cli(client, fake_config):
    hooks.main(["-t", "cleanup", "-c", str(fake_config), "-l", LINEAGE])

    assert len(client.call_args[0]) == 1
    resolver = client.call_args[0][0]

    assert resolver.resolve("lexicon:action") == "delete"
    assert resolver.resolve("lexicon:domain") == LINEAGE
    assert resolver.resolve("lexicon:type") == "TXT"
    assert resolver.resolve("lexicon:name") == "_acme-challenge.{0}.".format(LINEAGE)
    assert resolver.resolve("lexicon:content") == "VALIDATION"
    assert resolver.resolve("lexicon:provider_name") == "dummy"
    assert resolver.resolve("lexicon:dummy:auth_token") == "TOKEN"


@patch("dnsrobocert.core.hooks.deploy")
def test_deploy_cli(deploy, fake_config):
    hooks.main(["-t", "deploy", "-c", str(fake_config), "-l", LINEAGE])
    deploy.assert_called_with(config.load(fake_config), LINEAGE)


@patch("dnsrobocert.core.hooks._fix_permissions")
@patch("dnsrobocert.core.hooks._autocmd")
@patch("dnsrobocert.core.hooks._autorestart")
def test_pfx(_autorestart, _autocmd, _fix_permissions, fake_env, fake_config):
    archive_path = fake_env["archive"]
    key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    with open(archive_path / "privkey.pem", "wb") as f:
        f.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )

    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, u"example.com")]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=10))
        .sign(key, hashes.SHA256(), default_backend())
    )

    with open(archive_path / "cert.pem", "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(archive_path / "chain.pem", "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    hooks.deploy(config.load(fake_config), LINEAGE)

    assert os.path.exists(archive_path / "cert.pfx")
    assert os.stat(archive_path / "cert.pfx").st_size != 0


@patch("dnsrobocert.core.hooks._fix_permissions")
@patch("dnsrobocert.core.hooks._pfx_export")
@patch("dnsrobocert.core.hooks._autorestart")
@patch("dnsrobocert.core.hooks.os.path.exists")
@patch("dnsrobocert.core.hooks.utils.execute")
def test_autocmd(
    check_call, _exists, _autorestart, _pfx_export, _fix_permissions, fake_config
):
    hooks.deploy(config.load(fake_config), LINEAGE)

    call_foo = call(["docker", "exec", "foo", "echo 'Hello World!'"])
    call_bar = call(["docker", "exec", "bar", "echo 'Hello World!'"])
    check_call.assert_has_calls([call_foo, call_bar])


@patch("dnsrobocert.core.hooks._fix_permissions")
@patch("dnsrobocert.core.hooks._pfx_export")
@patch("dnsrobocert.core.hooks._autocmd")
@patch("dnsrobocert.core.hooks.os.path.exists")
@patch("dnsrobocert.core.hooks.utils.execute")
def test_autorestart(
    check_call, _exists, _autocmd, _pfx_export, _fix_permissions, fake_config
):
    hooks.deploy(config.load(fake_config), LINEAGE)

    call_container1 = call(["docker", "restart", "container1"])
    call_container2 = call(["docker", "restart", "container2"])
    call_service1 = call(
        ["docker", "service", "update", "--detach=false", "--force", "service1"]
    )
    call_service2 = call(
        ["docker", "service", "update", "--detach=false", "--force", "service2"]
    )
    check_call.assert_has_calls(
        [call_container1, call_container2, call_service1, call_service2]
    )


@patch("dnsrobocert.core.hooks._pfx_export")
@patch("dnsrobocert.core.hooks._autocmd")
@patch("dnsrobocert.core.hooks._autorestart")
def test_fix_permissions(_autorestart, _autocmd, _pfx_export, fake_config, fake_env):
    archive_path = fake_env["archive"]
    probe_file = os.path.join(archive_path, "dummy.txt")
    probe_dir = os.path.join(archive_path, "dummy_dir")
    open(probe_file, "w").close()
    os.mkdir(probe_dir)

    hooks.deploy(config.load(fake_config), LINEAGE)

    assert os.stat(probe_file).st_mode & 0o777 == 0o666
    assert os.stat(probe_dir).st_mode & 0o777 == 0o777
    assert os.stat(archive_path).st_mode & 0o777 == 0o777

    if POSIX_MODE:
        assert pwd.getpwuid(os.stat(probe_file).st_uid).pw_name == "nobody"
        assert grp.getgrgid(os.stat(probe_file).st_gid).gr_name == "nogroup"
        assert pwd.getpwuid(os.stat(probe_dir).st_uid).pw_name == "nobody"
        assert grp.getgrgid(os.stat(probe_dir).st_gid).gr_name == "nogroup"
        assert pwd.getpwuid(os.stat(archive_path).st_uid).pw_name == "nobody"
        assert grp.getgrgid(os.stat(archive_path).st_gid).gr_name == "nogroup"
