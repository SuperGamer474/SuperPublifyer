import sys
import asyncio
import json
import re
import threading
import time

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QPushButton, QVBoxLayout, QMessageBox
)
from PyQt6.QtCore import Qt, pyqtSignal, QObject

import websockets
import httpx


# Regex patterns
PROJECT_PATTERN = r"^[a-zA-Z0-9_-]{3,50}$"
LOCAL_URL_PATTERN = r"^[\w.-]+:\d+$"


def validate_input(val, pattern):
    return re.match(pattern, val) is not None


async def detect_protocol(local_url):
    for proto in ["https", "http"]:
        test_url = f"{proto}://{local_url}"
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(test_url)
                if resp.status_code < 400:
                    return proto
        except Exception:
            pass
    return "http"


class Communicate(QObject):
    update_status = pyqtSignal(str, str)  # text, colour
    show_error = pyqtSignal(str, str)  # title, message
    enable_button = pyqtSignal(bool)


class SuperPublifyerClientGUI(QWidget):
    def __init__(self):
        super().__init__()

        self.ws = None
        self.message_queue = None
        self.signals = Communicate()

        self.setWindowTitle("SuperPublifyer Client 🚀")
        self.setGeometry(300, 300, 400, 200)

        self.init_ui()

        # Connect signals to slots
        self.signals.update_status.connect(self.set_status)
        self.signals.show_error.connect(self.show_error_dialog)
        self.signals.enable_button.connect(self.connect_button.setEnabled)

    def init_ui(self):
        layout = QVBoxLayout()

        # Project Name
        self.project_label = QLabel("Project Name:")
        self.project_entry = QLineEdit()
        layout.addWidget(self.project_label)
        layout.addWidget(self.project_entry)

        # Local Server URL
        self.local_url_label = QLabel("Local Server URL (host:port):")
        self.local_url_entry = QLineEdit()
        layout.addWidget(self.local_url_label)
        layout.addWidget(self.local_url_entry)

        # Status Label
        self.status_label = QLabel("Status: Not connected")
        self.status_label.setStyleSheet("color: red;")
        layout.addWidget(self.status_label)

        # Connect Button
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self.on_connect)
        layout.addWidget(self.connect_button)

        self.setLayout(layout)

    def set_status(self, text, colour):
        self.status_label.setText(f"Status: {text}")
        self.status_label.setStyleSheet(f"color: {colour};")

    def show_error_dialog(self, title, message):
        QMessageBox.critical(self, title, message)

    def on_connect(self):
        # Disable button immediately
        self.connect_button.setEnabled(False)

        # Run async connect in a separate thread
        threading.Thread(target=lambda: asyncio.run(self.connect_async()), daemon=True).start()

    async def connect_async(self):
        project = self.project_entry.text().strip()
        local_url = self.local_url_entry.text().strip()

        # Validate inputs sync first to fail early
        if not validate_input(project, PROJECT_PATTERN):
            self.signals.show_error.emit(
                "Invalid Input", "Project name invalid! Use letters, numbers, _ or - only."
            )
            self.signals.enable_button.emit(True)
            return
        if not validate_input(local_url, LOCAL_URL_PATTERN):
            self.signals.show_error.emit(
                "Invalid Input", "Local URL invalid! Format must be host:port"
            )
            self.signals.enable_button.emit(True)
            return

        # Detect protocol async
        self.signals.update_status.emit("Detecting protocol...", "blue")
        protocol = await detect_protocol(local_url)
        self.signals.update_status.emit(f"Detected protocol: {protocol}", "blue")
        time.sleep(1)

        self.signals.update_status.emit("Connecting...", "orange")

        # Connect websocket with retry
        ws_uri = f"wss://superpublifyer.onrender.com/ws/{project}"
        http_url = "https://superpublifyer.onrender.com"

        for attempt in range(1, 6):
            try:
                self.signals.update_status.emit(f"Connecting... Attempt {attempt}", "orange")
                self.ws = await websockets.connect(ws_uri)
                break
            except Exception as e:
                if attempt == 5:
                    self.signals.show_error.emit("Connection Error", f"Failed to connect after 5 tries:\n{e}")
                    self.signals.update_status.emit("Error", "red")
                    self.signals.enable_button.emit(True)
                    return
                await asyncio.sleep(2 * attempt)  # backoff

        # Setup message queue and tasks
        self.message_queue = asyncio.Queue()
        asyncio.create_task(self.keep_alive())
        asyncio.create_task(self.ws_receiver())

        # Register project
        register_data = {
            "project_name": project,
            "local_url": local_url,
            "protocol": protocol,
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{http_url}/register", json=register_data)
            if resp.status_code != 200:
                self.signals.show_error.emit("Registration Failed", resp.text)
                self.signals.update_status.emit("Registration Failed", "red")
                self.signals.enable_button.emit(True)
                return
            else:
                self.signals.update_status.emit("Connected", "green")

        # Handle incoming proxy requests forever
        while True:
            request_msg = await self.message_queue.get()
            request = json.loads(request_msg)

            method = request.get("method")
            path = request.get("path")
            headers = request.get("headers")
            body = request.get("body")
            query = request.get("query")

            url = f"http://{local_url}/{path}"
            if query:
                url += f"?{query}"

            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        content=body.encode() if body else None,
                        timeout=60.0,
                    )
                response_data = {
                    "status_code": resp.status_code,
                    "headers": dict(resp.headers),
                    "body": resp.text,
                }
            except Exception as e:
                response_data = {
                    "status_code": 502,
                    "headers": {},
                    "body": f"Error forwarding request: {str(e)}",
                }

            await self.ws.send(json.dumps(response_data))

    async def keep_alive(self):
        try:
            while True:
                await self.ws.ping()
                await asyncio.sleep(15)
        except Exception:
            self.signals.update_status.emit("Disconnected", "red")
            self.signals.enable_button.emit(True)

    async def ws_receiver(self):
        try:
            async for msg in self.ws:
                await self.message_queue.put(msg)
        except websockets.ConnectionClosed:
            self.signals.update_status.emit("Disconnected", "red")
            self.signals.enable_button.emit(True)


def main():
    app = QApplication(sys.argv)
    gui = SuperPublifyerClientGUI()
    gui.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
