import json
import time
import unittest
from unittest.mock import patch

from webapp import create_app


class FakeChannel:
    def __init__(self, stdout: bytes, stderr: bytes = b"", exit_code: int = 0) -> None:
        self.stdout = bytearray(stdout)
        self.stderr = bytearray(stderr)
        self.exit_code = exit_code
        self.stdin_data = bytearray()
        self.stdin_closed = False

    def sendall(self, data: bytes) -> None:
        self.stdin_data.extend(data)

    def shutdown_write(self) -> None:
        self.stdin_closed = True

    def exit_status_ready(self) -> bool:
        return not self.stdout and not self.stderr

    def recv_ready(self) -> bool:
        return bool(self.stdout)

    def recv_stderr_ready(self) -> bool:
        return bool(self.stderr)

    def recv(self, size: int) -> bytes:
        data = bytes(self.stdout[:size])
        del self.stdout[:size]
        return data

    def recv_stderr(self, size: int) -> bytes:
        data = bytes(self.stderr[:size])
        del self.stderr[:size]
        return data

    def recv_exit_status(self) -> int:
        return self.exit_code

    def close(self) -> None:
        pass


class FakeStream:
    def __init__(self, channel: FakeChannel) -> None:
        self.channel = channel


class FakeSSHClient:
    def __init__(self, channel: FakeChannel) -> None:
        self.channel = channel
        self.connect_options = None
        self.command = None
        self.host_key_policy = None
        self.loaded_host_keys = []
        self.closed = False

    def load_system_host_keys(self) -> None:
        pass

    def load_host_keys(self, filename: str) -> None:
        self.loaded_host_keys.append(filename)

    def set_missing_host_key_policy(self, policy) -> None:
        self.host_key_policy = policy

    def connect(self, **options) -> None:
        self.connect_options = options

    def exec_command(self, command: str, timeout: int):
        self.command = command
        self.timeout = timeout
        return (
            FakeStream(self.channel),
            FakeStream(self.channel),
            FakeStream(self.channel),
        )

    def close(self) -> None:
        self.closed = True


class HermesKanbanRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "SITE_PASSWORD": "test-password",
                "KALI_VM_IP": "192.0.2.10",
                "KALI_SSH_PORT": 2222,
                "KALI_VM_USER": "kali",
                "KALI_SSH_KEY": r"C:\keys\kali",
                "KALI_SSH_KNOWN_HOSTS": r"C:\keys\known_hosts",
            }
        )
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["_csrf_token"] = "test-csrf"
            session["_last_activity"] = time.time()

    def test_home_page_renders_the_local_team_dashboard(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Your security team", response.data)
        self.assertIn(b"Team board", response.data)
        self.assertIn(b"security-lead", response.data)

    def test_board_returns_hermes_tasks_over_verified_vm_ssh(self) -> None:
        tasks = [{"id": "task-1", "title": "Review lab", "status": "triage"}]
        ssh = self._fake_ssh(tasks)
        with patch("webapp.paramiko.SSHClient", return_value=ssh):
            response = self.client.get("/api/team")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["tasks"], tasks)
        self.assertEqual(
            ssh.command,
            "hermes kanban --board default list --json",
        )
        self.assertEqual(ssh.connect_options["hostname"], "192.0.2.10")
        self.assertEqual(ssh.connect_options["port"], 2222)
        self.assertEqual(ssh.connect_options["username"], "kali")
        self.assertFalse(ssh.connect_options["look_for_keys"])
        self.assertEqual(ssh.loaded_host_keys, [r"C:\keys\known_hosts"])
        self.assertEqual(type(ssh.host_key_policy).__name__, "RejectPolicy")
        self.assertTrue(ssh.closed)

    def test_board_rejects_unrecognized_task_shapes(self) -> None:
        ssh = self._fake_ssh(
            [{"id": "task-1", "title": "Unknown task state", "status": "unexpected"}]
        )
        with patch("webapp.paramiko.SSHClient", return_value=ssh):
            response = self.client.get("/api/team")

        self.assertEqual(response.status_code, 502)
        self.assertIn("unexpected task list", response.json["error"])

    def test_task_creation_requires_authorization_confirmation(self) -> None:
        with patch("webapp.paramiko.SSHClient") as ssh_factory:
            response = self.client.post(
                "/api/team/tasks",
                json={"prompt": "Review my lab", "authorized": False},
                headers={"X-CSRF-Token": "test-csrf"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("authorized", response.json["error"].lower())
        ssh_factory.assert_not_called()

    def test_task_creation_quotes_command_and_sends_prompt_over_stdin(self) -> None:
        task = {
            "id": "task-2",
            "title": "Authorized assessment: Review my lab; printf bad",
            "status": "triage",
        }
        ssh = self._fake_ssh(task)
        prompt = "Review my lab; printf bad\nTarget: 192.0.2.10"
        with patch("webapp.paramiko.SSHClient", return_value=ssh):
            response = self.client.post(
                "/api/team/tasks",
                json={"prompt": prompt, "authorized": True},
                headers={"X-CSRF-Token": "test-csrf"},
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["task"], task)
        self.assertEqual(
            ssh.command,
            "hermes kanban --board default create "
            "'Authorized assessment: Review my lab; printf bad' "
            "--body-file - --assignee security-lead --triage --json",
        )
        self.assertIn("printf bad", ssh.command)
        self.assertTrue(ssh.command.endswith("security-lead --triage --json"))
        self.assertIn(prompt.encode(), ssh.channel.stdin_data)
        self.assertTrue(ssh.channel.stdin_closed)

    def test_hermes_failure_is_returned_to_the_user(self) -> None:
        ssh = self._fake_ssh(None, exit_code=1, stderr="board unavailable")
        with patch("webapp.paramiko.SSHClient", return_value=ssh):
            response = self.client.get("/api/team")

        self.assertEqual(response.status_code, 502)
        self.assertIn("board unavailable", response.json["error"])

    def test_missing_vm_settings_are_reported_without_connecting(self) -> None:
        self.app.config.update(KALI_VM_IP="", KALI_VM_USER="")
        with patch("webapp.paramiko.SSHClient") as ssh_factory:
            response = self.client.get("/api/team")

        self.assertEqual(response.status_code, 502)
        self.assertIn("KALI_VM_IP", response.json["error"])
        ssh_factory.assert_not_called()

    @staticmethod
    def _fake_ssh(value, exit_code: int = 0, stderr: str = "") -> FakeSSHClient:
        output = json.dumps(value).encode("utf-8")
        return FakeSSHClient(FakeChannel(output, stderr.encode("utf-8"), exit_code))


if __name__ == "__main__":
    unittest.main()
