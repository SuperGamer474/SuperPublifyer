import sys
import asyncio
import json
import re
import threading
import time
import webbrowser

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QPushButton, QVBoxLayout, QMessageBox,
    QHBoxLayout, QFrame, QTextEdit, QScrollArea, QSpacerItem, QSizePolicy
)
from PyQt6.QtGui import QFont, QClipboard, QTextCursor
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QTimer

import websockets
import httpx

PROJECT_PATTERN = r"^[a-zA-Z0-9_-]{3,50}$"
LOCAL_URL_PATTERN = r"^[\w.-]+:\d+$"

def validate_input(val, pattern):
    return re.match(pattern, val) is not None

async def detect_protocol(local_url):
    for proto in ("https", "http"):
        test_url = f"{proto}://{local_url}"
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(test_url, follow_redirects=True)
                if resp.status_code < 400:
                    return proto
        except Exception:
            pass
    return "http"

class Communicate(QObject):
    update_status = pyqtSignal(str, str)   
    show_error = pyqtSignal(str, str)      
    enable_button = pyqtSignal(bool)
    show_urls = pyqtSignal(list)           
    clear_urls = pyqtSignal()

class URLRow(QWidget):
    """Row with a readonly URL, Copy and Open buttons, and optional badge text."""
    def __init__(self, url, badge_text=None, parent=None):
        super().__init__(parent)
        self.url = url
        layout = QHBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)

        self.url_field = QLineEdit(url)
        self.url_field.setReadOnly(True)

        self.badge = QLabel(badge_text) if badge_text else None

        self.copy_btn = QPushButton("Copy")
        self.open_btn = QPushButton("Open")
        self.feedback = QLabel("")

        layout.addWidget(self.url_field)
        if self.badge:
            layout.addWidget(self.badge)
        layout.addWidget(self.copy_btn)
        layout.addWidget(self.open_btn)
        layout.addWidget(self.feedback)
        self.setLayout(layout)

        self.copy_btn.clicked.connect(self.copy_to_clipboard)
        self.open_btn.clicked.connect(self.open_in_browser)

    def copy_to_clipboard(self):
        cb: QClipboard = QApplication.clipboard()
        cb.setText(self.url)
        self.feedback.setText("Copied")
        QTimer.singleShot(1200, lambda: self.feedback.setText(""))

    def open_in_browser(self):
        webbrowser.open(self.url)

class SuperPublifyerClientGUI(QWidget):
    def __init__(self):
        super().__init__()
        self.ws = None
        self.message_queue = None
        self.signals = Communicate()

        self.setWindowTitle("SuperPublifyer — Client")
        self.resize(800, 520)
        self.init_ui()

        self.signals.update_status.connect(self.set_status)
        self.signals.show_error.connect(self.show_error_dialog)
        self.signals.enable_button.connect(self.connect_button.setEnabled)
        self.signals.show_urls.connect(self.populate_urls)
        self.signals.clear_urls.connect(self.clear_urls_area)

    def init_ui(self):
        main_layout = QVBoxLayout()
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(10, 10, 10, 10)

        header = QLabel("SuperPublifyer Client")
        header.setFont(QFont("", 16))
        main_layout.addWidget(header)

        subtitle = QLabel("Share your local dev server")
        main_layout.addWidget(subtitle)

        card = QFrame()
        card_layout = QVBoxLayout()
        card_layout.setSpacing(8)
        card_layout.setContentsMargins(8, 8, 8, 8)
        card.setLayout(card_layout)

        row_inputs = QHBoxLayout()
        self.project_entry = QLineEdit()
        self.project_entry.setPlaceholderText("Project name (e.g. my-project)")
        self.local_url_entry = QLineEdit()
        self.local_url_entry.setPlaceholderText("Local server (host:port) e.g. localhost:8000")
        row_inputs.addWidget(self.project_entry)
        row_inputs.addWidget(self.local_url_entry)
        card_layout.addLayout(row_inputs)

        row_buttons = QHBoxLayout()
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self.on_connect)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.clicked.connect(self.on_disconnect)
        self.disconnect_button.setEnabled(False)
        row_buttons.addWidget(self.connect_button)
        row_buttons.addWidget(self.disconnect_button)
        row_buttons.addStretch()
        card_layout.addLayout(row_buttons)

        status_label = QLabel("Status")
        self.status_area = QTextEdit()
        self.status_area.setReadOnly(True)
        self.status_area.setAcceptRichText(False)
        self.status_area.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.status_area.setFixedHeight(140)  
        card_layout.addWidget(status_label)
        card_layout.addWidget(self.status_area)

        main_layout.addWidget(card)

        urls_title = QLabel("Accessible URLs")
        main_layout.addWidget(urls_title)

        self.urls_container_frame = QFrame()
        v = QVBoxLayout()
        v.setSpacing(6)
        v.setContentsMargins(2, 2, 2, 2)
        self.urls_layout = QVBoxLayout()
        v.addLayout(self.urls_layout)
        v.addItem(QSpacerItem(20, 20, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding))
        self.urls_container_frame.setLayout(v)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.urls_container_frame)
        scroll.setFixedHeight(160)
        main_layout.addWidget(scroll)

        self.setLayout(main_layout)

    def set_status(self, text, colour="black"):
        ts = time.strftime("%H:%M:%S")
        append = f"[{ts}] {text}\n"

        self.status_area.moveCursor(QTextCursor.MoveOperation.End)
        self.status_area.insertPlainText(append)
        self.status_area.verticalScrollBar().setValue(self.status_area.verticalScrollBar().maximum())

    def show_error_dialog(self, title, message):
        QMessageBox.critical(self, title, message)

    def clear_urls_area(self):
        while self.urls_layout.count():
            item = self.urls_layout.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)

    def populate_urls(self, url_tuples):
        self.signals.clear_urls.emit()
        for url, badge in url_tuples:
            row = URLRow(url, badge)
            self.urls_layout.addWidget(row)

    def on_connect(self):
        self.connect_button.setEnabled(False)
        self.signals.update_status.emit("Starting connection...", "black")
        threading.Thread(target=lambda: asyncio.run(self.connect_async()), daemon=True).start()

    def on_disconnect(self):
        try:
            if self.ws:

                asyncio.run(self.ws.close())
        except Exception:
            pass
        self.signals.update_status.emit("Disconnected by user", "black")
        self.disconnect_button.setEnabled(False)
        self.connect_button.setEnabled(True)
        self.signals.clear_urls.emit()

    async def connect_async(self):
        project = self.project_entry.text().strip()
        local_url = self.local_url_entry.text().strip()

        if not validate_input(project, PROJECT_PATTERN):
            self.signals.show_error.emit("Invalid Input", "Project name invalid! Use letters, numbers, _ or - only.")
            self.signals.enable_button.emit(True)
            return
        if not validate_input(local_url, LOCAL_URL_PATTERN):
            self.signals.show_error.emit("Invalid Input", "Local URL invalid! Format must be host:port")
            self.signals.enable_button.emit(True)
            return

        self.signals.update_status.emit("Detecting protocol...", "black")
        protocol = await detect_protocol(local_url)
        self.signals.update_status.emit(f"Detected protocol: {protocol}", "black")
        await asyncio.sleep(0.15)

        self.signals.update_status.emit("Connecting to service...", "black")

        ws_uri = f"wss://superpublifyer.onrender.com/ws/{project}"
        http_url = "https://superpublifyer.onrender.com"

        for attempt in range(1, 6):
            try:
                self.signals.update_status.emit(f"WebSocket attempt {attempt}...", "black")
                self.ws = await websockets.connect(ws_uri)
                break
            except Exception as e:
                self.signals.update_status.emit(f"Attempt {attempt} failed: {e}", "black")
                if attempt == 5:
                    self.signals.show_error.emit("Connection Error", f"Failed to connect after 5 tries:\n{e}")
                    self.signals.update_status.emit("Error", "black")
                    self.signals.enable_button.emit(True)
                    return
                await asyncio.sleep(0.8 * attempt)

        self.message_queue = asyncio.Queue()
        asyncio.create_task(self.keep_alive())
        asyncio.create_task(self.ws_receiver())

        register_data = {"project_name": project, "local_url": local_url, "protocol": protocol}
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(f"{http_url}/register", json=register_data, timeout=10.0)
            except Exception as e:
                self.signals.show_error.emit("Registration Failed", f"Could not talk to service: {e}")
                self.signals.update_status.emit("Registration Failed", "black")
                self.signals.enable_button.emit(True)
                return

            if resp.status_code != 200:
                self.signals.show_error.emit("Registration Failed", resp.text)
                self.signals.update_status.emit("Registration Failed", "black")
                self.signals.enable_button.emit(True)
                return

        urls = []
        host_part = local_url.split(":", 1)[0].lower()
        port_part = local_url.split(":", 1)[1] if ":" in local_url else ""
        if host_part in ("localhost", "127.0.0.1"):
            urls.append((f"{protocol}://localhost:{port_part}/", "(Local only)"))
            urls.append((f"{protocol}://127.0.0.1:{port_part}/", "(Local only)"))
        else:
            urls.append((f"{protocol}://{local_url}/", "(Local only)"))
        urls.append((f"http://public.servehttp.com/{project}/", "Public - May not work"))
        urls.append((f"https://superpublifyer.onrender.com/{project}/", "Public"))

        self.signals.show_urls.emit(urls)
        accessible_msg = "Connected — your server is accessible from:\n" + "\n".join(u for u, _ in urls)
        self.signals.update_status.emit(accessible_msg, "black")

        self.disconnect_button.setEnabled(True)

        while True:
            try:
                request_msg = await self.message_queue.get()
            except Exception:
                break
            try:
                request = json.loads(request_msg)
                method = request.get("method")
                path = request.get("path", "")
                headers = request.get("headers", {})
                body = request.get("body")
                query = request.get("query")
                forward_url = f"http://{local_url}/{path.lstrip('/')}"
                if query:
                    forward_url += f"?{query}"
                async with httpx.AsyncClient() as client:
                    resp = await client.request(
                        method=method,
                        url=forward_url,
                        headers=headers,
                        content=body.encode() if body else None,
                        timeout=60.0,
                    )
                response_data = {"status_code": resp.status_code, "headers": dict(resp.headers), "body": resp.text}
            except Exception as e:
                response_data = {"status_code": 502, "headers": {}, "body": f"Error forwarding request: {e}"}
            try:
                await self.ws.send(json.dumps(response_data))
            except Exception:
                break

    async def keep_alive(self):
        try:
            while True:
                if self.ws:
                    await self.ws.ping()
                await asyncio.sleep(15)
        except Exception:
            self.signals.update_status.emit("Connection lost (keep-alive failed)", "black")
            self.signals.enable_button.emit(True)

    async def ws_receiver(self):
        try:
            async for msg in self.ws:
                await self.message_queue.put(msg)
        except websockets.ConnectionClosed:
            self.signals.update_status.emit("Disconnected", "black")
            self.signals.enable_button.emit(True)
            self.signals.clear_urls.emit()
        except Exception as e:
            self.signals.update_status.emit(f"Receiver error: {e}", "black")
            self.signals.enable_button.emit(True)
            self.signals.clear_urls.emit()

def main():
    app = QApplication(sys.argv)
    gui = SuperPublifyerClientGUI()
    gui.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
