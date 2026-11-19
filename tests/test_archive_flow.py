from http.client import HTTPConnection
from io import BytesIO
import json
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from zipfile import ZipFile

from flame2resolve import archives, auth
from flame2resolve.server import Application
from make_fixture import make_fixture


class ArchiveFlowTests(unittest.TestCase):
    def test_synthetic_archive_has_saved_versions_resize_and_timewarp(self):
        with TemporaryDirectory() as directory:
            header, _ = make_fixture(Path(directory) / "demo")
            listing, plans = archives.inspect(header)
            sequence, = listing
            self.assertEqual(sequence["frames"], 20)
            plan = plans[sequence["id"]]
            self.assertEqual((plan.start, plan.fps, plan.width, plan.height), (90000, 25, 1920, 1080))
            self.assertEqual([cut.version for cut in plan.cuts], ["v003", "v003"])
            self.assertEqual([cut.record for cut in plan.cuts], [0, 12])
            self.assertEqual(plan.cuts[0].settings["ResizeFilter"], 10)
            self.assertEqual(plan.cuts[1].retime.speed, 125)

    def test_authenticated_upload_mapping_export_and_cleanup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            files = make_fixture(root / "demo")
            password = secrets.token_urlsafe(18)
            with Application("127.0.0.1", 0, root / "uploads", auth.make_verifier(password), "") as server:
                host, port = server.server_address
                server.origin = f"http://{host}:{port}"
                worker = Thread(target=server.serve_forever)
                worker.start()
                try:
                    self.exercise_http(server, files, password)
                finally:
                    server.shutdown()
                    server.done.set()
                    worker.join()

    def exercise_http(self, server, files, password):
        cookie = ""

        def request(method, path, status=200, payload=None, body=None, send_cookie=True):
            nonlocal cookie
            headers = {"Origin": server.origin}
            if send_cookie and cookie:
                headers["Cookie"] = cookie
            if payload is not None:
                headers["Content-Type"] = "application/json"
                body = json.dumps(payload).encode()
            elif body is not None:
                headers.update({"Content-Type": "application/octet-stream", "X-Offset": "0"})
            connection = HTTPConnection(*server.server_address, timeout=5)
            try:
                connection.request(method, path, body, headers)
                response = connection.getresponse()
                data = response.read()
                self.assertEqual(response.status, status, (method, path, data))
                if value := response.getheader("Set-Cookie"):
                    cookie = value.split(";")[0]
                if response.getheader("Content-Type").startswith("application/json"):
                    return json.loads(data)
                return data
            finally:
                connection.close()

        self.assertEqual(request("GET", "/api/session"), {"authenticated": False})
        request("POST", "/api/upload", 401, payload={"files": []})
        request("POST", "/api/login", payload={"password": password})
        request("POST", "/api/upload", 413, payload={"files": [
            {"name": "demo", "size": 1_000_000_000}, {"name": "demo_1.seg", "size": 1}
        ]})
        upload = request("POST", "/api/upload", 201, payload={"files": [
            {"name": path.name, "size": path.stat().st_size} for path in files
        ]})
        job = "/api/upload/" + upload["id"]
        for index, path in enumerate(files):
            request("POST", f"{job}/{index}", body=path.read_bytes())
        listing = request("POST", f"{job}/inspect")
        self.assertEqual(list(server.workspace.glob("*/*")), [])
        self.assertEqual(listing["bases"], ["/old/demo/renders/"])
        reply = request("POST", f"{job}/export", payload={
            "timeline": listing["timelines"][0]["id"], "scale": "fit", "remap": True,
            "mappings": [{"source": listing["bases"][0], "target": "/Volumes/new/demo/"}]
        })
        self.assertEqual(reply["rows"][0]["destination"], "/Volumes/new/demo/alpha/alpha_v003.[1001-1040].exr")
        request("GET", reply["url"], 401, send_cookie=False)
        with ZipFile(BytesIO(request("GET", reply["url"]))) as drt:
            self.assertIsNone(drt.testzip())
            self.assertIn("project.xml", drt.namelist())
        request("DELETE", job)
        request("GET", reply["url"], 404)
