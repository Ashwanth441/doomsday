"""API-level marketplace checks; provider calls are intentionally not faked."""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
import unittest
import uuid
import zipfile
from email.message import Message
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PROJECT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DOOMSDAY_DB_PATH", "/private/tmp/doomsday-tests-bootstrap.sqlite3")
sys.path.insert(0, str(PROJECT))
import server  # noqa: E402


class PostgreSQLSchemaTests(unittest.TestCase):
    def test_selected_offer_foreign_key_is_added_after_offers_table(self):
        schema = server.postgres_schema()
        task_table = schema.split("CREATE TABLE IF NOT EXISTS tasks (", 1)[1].split(");", 1)[0]
        self.assertIn("selected_offer_id BIGINT,", task_table)
        self.assertNotIn("REFERENCES offers", task_table)
        self.assertLess(
            schema.index("CREATE TABLE IF NOT EXISTS offers ("),
            schema.index("CREATE TABLE IF NOT EXISTS task_assignments ("),
        )
        self.assertIn("REFERENCES offers(id)", server.POSTGRES_SELECTED_OFFER_FK)


class TestHandler(server.Handler):
    def send_json(self, status, payload):
        self.response = (status, payload)

    def send_blob(self, data, mime):
        self.response = (200, {"blob": data, "mime": mime})

    def send_private_file(self, data, mime, filename, disposition):
        self.response = (200, {"blob": data, "mime": mime, "filename": filename, "disposition": disposition})


class MarketplaceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="doomsday-test-")
        server.DB_PATH = Path(self.tmp.name) / "marketplace.sqlite3"
        server.init_db()
        self.customer, self.customer_token = self.user("+919876543210", "Priya Customer")
        self.tasker, self.tasker_token = self.user("+919876543211", "Arun Tasker")
        self.second, self.second_token = self.user("+919876543212", "Meena Tasker")

    def tearDown(self):
        self.tmp.cleanup()

    def user(self, phone: str, name: str) -> tuple[str, str]:
        user_id = str(uuid.uuid4())
        token = "test-session-" + uuid.uuid4().hex
        stamp = server.now_iso()
        from datetime import timedelta
        from datetime import datetime, timezone
        expires = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with server.db() as conn:
            conn.execute("INSERT INTO users(id,phone,name,city,lat,lng,joined_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (user_id,phone,name,"Chennai",13.08,80.27,stamp,stamp))
            conn.execute("INSERT INTO user_verifications(user_id,kind,status,verified_at) VALUES(?,'phone','verified',?)", (user_id,stamp))
            conn.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)", (server.hash_token(token),user_id,stamp,expires))
        return user_id, token

    def request(self, method: str, path: str, token: str | None = None, body: dict | None = None, cookie_token: str | None = None):
        context = object.__new__(TestHandler)
        context.headers = Message()
        if token:
            context.headers["Authorization"] = "Bearer " + token
        if cookie_token:
            context.headers["Cookie"] = "doomsday_session=" + cookie_token
        context.client_address = ("127.0.0.1", 12345)
        context._raw_body = json.dumps(body or {}).encode()
        parsed = urlsplit(path)
        with server.db() as conn:
            try:
                if method == "GET":
                    context.api_get(conn, parsed.path, parse_qs(parsed.query))
                    return context.response
                if method == "POST" and parsed.path == "/api/webhooks/razorpay":
                    result, status = context.api_write(conn, method, parsed.path, body or {})
                elif method == "PATCH" and parsed.path.startswith("/api/admin/"):
                    result, status = context.admin_write(conn, server.session_user(conn, context), method, parsed.path, body or {})
                elif method == "PATCH" and parsed.path == "/api/notifications/read":
                    user = server.session_user(conn, context)
                    conn.execute("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL", (server.now_iso(), user["id"]))
                    result, status = {"ok": True}, 200
                else:
                    result, status = context.api_write(conn, method, parsed.path, body or {})
                return status, result
            except server.ApiError as exc:
                return exc.status, {"error": {"code": exc.code, "message": exc.message}}
            except Exception as exc:
                raise AssertionError(f"Unhandled API exception for {method} {path}: {exc}") from exc

    def create_task(self):
        return self.request("POST", "/api/tasks", self.customer_token, {
            "title": "Pick up my medicine",
            "description": "Collect my prescription from the pharmacy and bring it to my home this evening.",
            "category": "delivery-pickup",
            "address": "12 Private Lane, Adyar, Chennai",
            "landmark": "Beside the library",
            "city": "Chennai",
            "lat": 13.005,
            "lng": 80.256,
            "time_mode": "TODAY",
            "budget_mode": "NEGOTIABLE",
            "budget_min": 200,
            "budget_max": 500,
            "images": [],
        })

    def offer(self, task_id: str, token: str, amount: int = 250):
        return self.request("POST", f"/api/tasks/{task_id}/offers", token, {
            "amount": amount,
            "message": "I can collect and deliver this safely today.",
            "eta_minutes": 35,
        })

    def create_study_task(self, *, acknowledged: bool = True):
        self.request("PATCH", "/api/me", self.customer_token, {"is_student": True})
        return self.request("POST", "/api/tasks", self.customer_token, {
            "title": "Create a mental health study presentation",
            "description": "Prepare a short educational presentation with helpful mental health resources for a student club.",
            "category": "student-study-support",
            "city": "Chennai",
            "time_mode": "TODAY",
            "budget_mode": "FIXED",
            "budget_min": 100,
            "budget_max": 100,
            "integrity_acknowledged": acknowledged,
            "images": [],
        })

    @staticmethod
    def data_url(filename: str, mime: str, content: bytes) -> dict:
        encoded = base64.b64encode(content).decode("ascii")
        return {"name": filename, "data": f"data:{mime};base64,{encoded}"}

    def test_task_discovery_search_and_address_privacy(self):
        status, task = self.create_task()
        self.assertEqual(status, 201)
        task_id = task["task_id"]
        anonymous_code, anonymous = self.request("GET", f"/api/tasks/{task_id}")
        self.assertEqual(anonymous_code, 401)
        self.assertEqual(anonymous["error"]["code"], "sign_in_required")
        code, result = self.request("GET", "/api/tasks?lat=13.08&lng=80.27&radius=10&q=medicine", self.tasker_token)
        self.assertEqual(code, 200)
        self.assertEqual(len(result["tasks"]), 1)
        card = result["tasks"][0]
        self.assertNotIn("customer_id", card)
        self.assertNotIn("assigned_tasker_id", card)
        code, detail = self.request("GET", f"/api/tasks/{task_id}", self.tasker_token)
        self.assertEqual(code, 200)
        self.assertEqual(detail["task"]["address"], "Shared after a tasker is selected")
        self.assertIsNone(detail["task"]["lat"])
        self.assertAlmostEqual(card["public_lat"], 13.01, places=2)

    def test_offer_selection_chat_and_payment_gated_state_machine(self):
        _, task = self.create_task()
        task_id = task["task_id"]
        code, _ = self.request("POST", f"/api/tasks/{task_id}/messages", self.tasker_token, {"body": "Can I ask a question?"})
        self.assertEqual(code, 403)
        code, first = self.offer(task_id, self.tasker_token)
        self.assertEqual(code, 201)
        code, second = self.offer(task_id, self.second_token, 300)
        self.assertEqual(code, 201)
        code, _ = self.offer(task_id, self.tasker_token)
        self.assertEqual(code, 409)
        code, offers = self.request("GET", f"/api/tasks/{task_id}/offers", self.tasker_token)
        self.assertEqual(code, 200)
        self.assertEqual(len(offers["offers"]), 1, "Taskers must not see competitors' offers.")
        code, selected = self.request("POST", f"/api/offers/{first['offer_id']}/select", self.customer_token, {})
        self.assertEqual(code, 200)
        self.assertEqual(selected["status"], "ASSIGNED")
        code, private_task = self.request("GET", f"/api/tasks/{task_id}", self.second_token)
        self.assertEqual(code, 404, "Unselected taskers must not inspect assigned tasks.")
        code, chat = self.request("POST", f"/api/tasks/{task_id}/messages", self.tasker_token, {"body": "I am ready to help."})
        self.assertEqual(code, 201)
        self.assertEqual(chat["message"], "Message sent.")
        code, detail = self.request("GET", f"/api/tasks/{task_id}", self.tasker_token)
        self.assertEqual(code, 200)
        self.assertEqual(detail["task"]["address"], "12 Private Lane, Adyar, Chennai")
        self.assertEqual(detail["task"]["payment_status"], "not_paid")
        code, denied = self.request("POST", f"/api/tasks/{task_id}/status", self.tasker_token, {"status": "TASKER_ON_THE_WAY"})
        self.assertEqual(code, 409)
        self.assertEqual(denied["error"]["code"], "payment_required")
        code, _ = self.request("POST", f"/api/tasks/{task_id}/status", self.second_token, {"status": "TASKER_ON_THE_WAY"})
        self.assertEqual(code, 403)

    def test_provider_credentials_are_required_and_no_payment_is_faked(self):
        _, task = self.create_task()
        _, offer = self.offer(task["task_id"], self.tasker_token)
        self.request("POST", f"/api/offers/{offer['offer_id']}/select", self.customer_token, {})
        code, response = self.request("POST", "/api/payments/order", self.customer_token, {"task_id": task["task_id"]})
        self.assertEqual(code, 503)
        self.assertEqual(response["error"]["code"], "payments_unavailable")
        self.assertIn("not been charged", response["error"]["message"])

    def test_student_study_support_requires_ack_and_posts_without_private_address(self):
        code, response = self.request("POST", "/api/tasks", self.customer_token, {
            "title": "Create a mental health study presentation",
            "description": "Prepare a short educational presentation with helpful mental health resources for a student club.",
            "category": "student-study-support", "integrity_acknowledged": True,
            "city": "Chennai", "time_mode": "TODAY", "budget_mode": "FIXED",
            "budget_min": 100, "budget_max": 100, "images": [],
        })
        self.assertEqual(code, 403)
        self.assertEqual(response["error"]["code"], "student_access_required")

        code, response = self.create_study_task(acknowledged=False)
        self.assertEqual(code, 400)
        self.assertEqual(response["error"]["code"], "study_support_ack_required")

        code, created = self.create_study_task()
        self.assertEqual(code, 201)
        code, detail = self.request("GET", f"/api/tasks/{created['task_id']}", self.tasker_token)
        self.assertEqual(code, 200)
        task = detail["task"]
        self.assertTrue(task["is_digital"])
        self.assertIsNone(task["distance_km"])
        self.assertEqual(task["address"], "Shared after a tasker is selected")
        self.assertIsNone(task["lat"])
        self.assertNotIn("public_lat", task)
        self.assertNotIn("public_lng", task)
        with server.db() as conn:
            conn.execute("UPDATE users SET city='Madurai',lat=9.9252,lng=78.1198 WHERE id=?", (self.tasker,))
        code, nearby = self.request("GET", "/api/tasks?lat=9.9252&lng=78.1198&radius=2", self.tasker_token)
        self.assertEqual(code, 200)
        self.assertEqual([item["public_id"] for item in nearby["tasks"]], [created["task_id"]])
        self.assertTrue(nearby["tasks"][0]["is_digital"])

    def test_student_submission_is_selected_tasker_only_and_original_stays_locked_until_payout(self):
        code, task = self.create_study_task()
        self.assertEqual(code, 201)
        task_id = task["task_id"]
        code, offer = self.offer(task_id, self.tasker_token, 100)
        self.assertEqual(code, 201)
        code, _ = self.request("POST", f"/api/offers/{offer['offer_id']}/select", self.customer_token, {})
        self.assertEqual(code, 200)
        message_body={"attachment":self.data_url("guide.pdf","application/pdf",b"%PDF-1.4\nextra guide")}
        code, message = self.request("POST", f"/api/tasks/{task_id}/messages", self.tasker_token, message_body)
        self.assertEqual(code, 201)
        _, chat = self.request("GET", f"/api/tasks/{task_id}/messages", self.customer_token)
        chat_file=chat["messages"][0]
        self.assertTrue(chat_file["attachment_locked"])
        self.assertEqual(chat_file["attachment_url"],"")
        code, locked_chat_file = self.request("GET", f"/api/messages/{chat_file['id']}/attachment", self.customer_token)
        self.assertEqual(code, 402)
        self.assertEqual(locked_chat_file["error"]["code"],"attachment_locked")

        office_file = io.BytesIO()
        with zipfile.ZipFile(office_file, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("ppt/presentation.xml", "<p:presentation/>")
        body = {
            "original": self.data_url("mental-health.pptx", server.UPLOAD_TYPES[".pptx"], office_file.getvalue()),
            "preview": self.data_url("preview.pdf", "application/pdf", b"%PDF-1.4\npreview"),
        }
        code, denied = self.request("POST", f"/api/tasks/{task_id}/submission", self.second_token, body)
        self.assertEqual(code, 403)
        self.assertEqual(denied["error"]["code"], "submission_access_denied")

        code, sent = self.request("POST", f"/api/tasks/{task_id}/submission", self.tasker_token, body)
        self.assertEqual(code, 201)
        self.assertEqual(sent["status"], "COMPLETED_BY_TASKER")
        code, detail = self.request("GET", f"/api/tasks/{task_id}", self.customer_token)
        self.assertEqual(code, 200)
        self.assertTrue(detail["task"]["has_submission"])
        self.assertFalse(detail["task"]["download_unlocked"])

        code, preview = self.request("GET", f"/api/tasks/{task_id}/submission/preview", self.customer_token)
        self.assertEqual(code, 200)
        self.assertEqual(preview["mime"], "application/pdf")
        self.assertEqual(preview["disposition"], "inline")
        code, _ = self.request("GET", f"/api/tasks/{task_id}/submission/preview", self.tasker_token)
        self.assertEqual(code, 403)
        code, locked = self.request("GET", f"/api/tasks/{task_id}/submission/download", self.customer_token)
        self.assertEqual(code, 402)
        self.assertEqual(locked["error"]["code"], "download_locked")

        with server.db() as conn:
            task_row = conn.execute("SELECT id,assigned_tasker_id FROM tasks WHERE public_id=?", (task_id,)).fetchone()
            conn.execute("INSERT INTO payments(public_id,task_id,customer_id,tasker_id,amount_paise,fee_paise,payout_paise,status,transfer_status,created_at,paid_at,released_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()),task_row["id"],self.customer,self.tasker,10000,0,10000,"transfer_created","created",server.now_iso(),server.now_iso(),server.now_iso(),server.now_iso()))
            conn.execute("UPDATE tasks SET status='PAYMENT_RELEASED' WHERE id=?", (task_row["id"],))
        code, still_locked = self.request("GET", f"/api/tasks/{task_id}/submission/download", self.customer_token)
        self.assertEqual(code, 402)
        self.assertEqual(still_locked["error"]["code"], "download_locked")
        _, pending = self.request("GET", f"/api/tasks/{task_id}", self.customer_token)
        self.assertFalse(pending["task"]["download_unlocked"])
        with server.db() as conn:
            conn.execute("UPDATE payments SET transfer_status='settled' WHERE task_id=(SELECT id FROM tasks WHERE public_id=?)", (task_id,))
        code, downloaded = self.request("GET", f"/api/tasks/{task_id}/submission/download", self.customer_token)
        self.assertEqual(code, 200)
        self.assertEqual(downloaded["disposition"], "attachment")
        self.assertEqual(downloaded["filename"], "mental-health.pptx")
        _, unlocked_chat = self.request("GET", f"/api/tasks/{task_id}/messages", self.customer_token)
        self.assertTrue(unlocked_chat["messages"][0]["attachment_url"])
        code, shared_file = self.request("GET", unlocked_chat["messages"][0]["attachment_url"], self.customer_token)
        self.assertEqual(code, 200)
        self.assertEqual(shared_file["filename"],"guide.pdf")

    def test_student_offer_price_is_paid_in_full_and_platform_fee_is_added_to_checkout(self):
        fee, payout, customer_total = server.calculate_payment_split(10_000,"10","0","100000",digital=True)
        self.assertEqual((fee,payout,customer_total),(1_000,10_000,11_000))
        fee, payout, customer_total = server.calculate_payment_split(10_000,"10","0","100000",digital=False)
        self.assertEqual((fee,payout,customer_total),(1_000,9_000,10_000))
        fee, payout, customer_total = server.calculate_payment_split(10_000,"10","0","100000","50",True)
        self.assertEqual((fee,payout,customer_total),(500,10_000,10_500))

    def test_task_chat_accepts_private_documents_and_blocks_nonparticipants(self):
        _, task = self.create_task()
        _, offer = self.offer(task["task_id"], self.tasker_token)
        self.request("POST", f"/api/offers/{offer['offer_id']}/select", self.customer_token, {})
        attachment = self.data_url("notes.pdf", "application/pdf", b"%PDF-1.4\nchat file")
        code, result = self.request("POST", f"/api/tasks/{task['task_id']}/messages", self.tasker_token, {"attachment": attachment})
        self.assertEqual(code, 201)
        code, messages = self.request("GET", f"/api/tasks/{task['task_id']}/messages", self.customer_token)
        self.assertEqual(code, 200)
        item = messages["messages"][0]
        self.assertEqual(item["attachment_name"], "notes.pdf")
        code, file = self.request("GET", item["attachment_url"], self.customer_token)
        self.assertEqual(code, 200)
        self.assertEqual(file["blob"], b"%PDF-1.4\nchat file")
        code, denied = self.request("GET", item["attachment_url"], self.second_token)
        self.assertEqual(code, 404)
        self.assertEqual(denied["error"]["code"], "attachment_not_found")

    def test_invalid_otp_and_unauthenticated_requests_fail_closed(self):
        code, response = self.request("POST", "/api/auth/verify", body={"phone": "9876543210", "code": "not-a-code"})
        self.assertEqual(code, 400)
        self.assertEqual(response["error"]["code"], "invalid_code")
        code, response = self.request("GET", "/api/tasks")
        self.assertEqual(code, 401)
        self.assertEqual(response["error"]["code"], "sign_in_required")

    def test_user_block_prevents_offer(self):
        _, task = self.create_task()
        code, blocked = self.request("POST", f"/api/users/{self.customer}/block", self.tasker_token, {})
        self.assertEqual(code, 200)
        self.assertTrue(blocked["blocked"])
        code, result = self.offer(task["task_id"], self.tasker_token)
        self.assertEqual(code, 403)
        self.assertEqual(result["error"]["code"], "user_blocked")
        code, _ = self.request("GET", f"/api/tasks/{task['task_id']}", self.tasker_token)
        self.assertEqual(code, 404)

    def test_cookie_sessions_and_public_profile_keep_private_fields_private(self):
        code, tasks = self.request("GET", "/api/my/tasks", cookie_token=self.customer_token)
        self.assertEqual(code, 200)
        code, profile = self.request("GET", f"/api/profiles/{self.customer}", self.tasker_token)
        self.assertEqual(code, 200)
        public = profile["profile"]
        for field in ("phone", "email", "lat", "lng", "address", "payout_connected"):
            self.assertNotIn(field, public)
        self.assertTrue(public["phone_verified"])

    def test_duplicate_reviews_are_rejected_as_a_user_error(self):
        _, task = self.create_task()
        _, offer = self.offer(task["task_id"], self.tasker_token)
        self.request("POST", f"/api/offers/{offer['offer_id']}/select", self.customer_token, {})
        with server.db() as conn:
            conn.execute("UPDATE tasks SET status='PAYMENT_RELEASED' WHERE public_id=?", (task["task_id"],))
        code, _ = self.request("POST", f"/api/tasks/{task['task_id']}/rate", self.customer_token, {"score":5,"review":"Quick and thoughtful."})
        self.assertEqual(code, 201)
        code, response = self.request("POST", f"/api/tasks/{task['task_id']}/rate", self.customer_token, {"score":4,"review":"Updating my review."})
        self.assertEqual(code, 409)
        self.assertEqual(response["error"]["code"], "duplicate_review")

    def test_reports_and_disputes_are_saved_and_freeze_task_progress(self):
        _, task = self.create_task()
        code, report = self.request("POST", f"/api/tasks/{task['task_id']}/report", self.tasker_token, {"reason":"safety","description":"The task details need a safety review."})
        self.assertEqual(code, 201)
        self.assertTrue(report["reported"])
        code, dispute = self.request("POST", f"/api/tasks/{task['task_id']}/dispute", self.customer_token, {"reason":"safety","description":"I need support to review this request."})
        self.assertEqual(code, 201)
        self.assertEqual(dispute["status"], "OPEN")
        code, detail = self.request("GET", f"/api/tasks/{task['task_id']}", self.customer_token)
        self.assertEqual(detail["task"]["status"], "DISPUTED")
        code, blocked_offer = self.offer(task["task_id"], self.tasker_token)
        self.assertEqual(code, 409)
        self.assertEqual(blocked_offer["error"]["code"], "task_not_open")

    def test_admin_routes_are_not_available_to_regular_users_and_bad_images_fail(self):
        code, response = self.request("GET", "/api/admin/overview", self.customer_token)
        self.assertEqual(code, 403)
        self.assertEqual(response["error"]["code"], "admin_only")
        with self.assertRaises(server.ApiError) as error:
            server.clean_image({"data":"data:image/png;base64,SGVsbG8="})
        self.assertEqual(error.exception.code, "invalid_image")


if __name__ == "__main__":
    unittest.main(verbosity=2)
