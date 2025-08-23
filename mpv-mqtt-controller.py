#!/usr/bin/python3
import os
import json
import re
import subprocess
import socket
import paho.mqtt.client as mqtt

class MPVController:
    def __init__(self, config_path="./config.json"):
        self.config = self.load_config(config_path)
        self.mpv_process = None
        self.playlist = []
        self.is_playing = False

    def load_config(self, path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Config load error: {e}")
            raise SystemExit(1)

    def natural_sort_key(self, s):
        """
        Генерирует ключ для естественной сортировки (natural sort)
        """
        import re
        return [int(text) if text.isdigit() else text.lower()
                for text in re.split(r'(\d+)', s)]

    def prepare_playlist(self):
        try:
            media_dir = self.config["mpv"]["media_dir"]
            if not os.path.exists(media_dir):
                raise FileNotFoundError(f"Media directory {media_dir} not found")

            # Получаем список файлов, соответствующих шаблону
            files = [
                os.path.join(media_dir, f)
                for f in os.listdir(media_dir)
                if re.fullmatch(self.config["mpv"]["file_pattern"], f)
            ]

            if not files:
                print("No media files found matching the pattern")
                return False

            # Сортируем файлы с естественной сортировкой
            self.playlist = sorted(files, key=lambda x: self.natural_sort_key(os.path.basename(x)))

            with open(self.config["mpv"]["playlist_file"], "w") as f:
                f.write("\n".join(self.playlist))

            print(f"Playlist created with {len(self.playlist)} files")
            return True

        except Exception as e:
            print(f"Playlist error: {e}")
            return False

    def start_mpv_drm(self):
        if self.mpv_process and self.mpv_process.poll() is None:
            print("MPV is already running")
            return True

        base_cmd = [
            "mpv",
            f"--input-ipc-server={self.config['mpv']['socket_path']}",
            "--vo=drm",
            "--audio-device=alsa/default",
            "--no-input-default-bindings",
            "--no-input-cursor",
            "--profile=sw-fast",
            f"--playlist={self.config['mpv']['playlist_file']}",
            "--loop-playlist"
        ]

        try:
            self.mpv_process = subprocess.Popen(
                base_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True
            )
            self.is_playing = True
            print("MPV started successfully")
            return True

        except Exception as e:
            print(f"MPV start failed: {e}")
            self.is_playing = False
            return False

    def stop_mpv(self):
        if self.mpv_process:
            try:
                self.mpv_process.terminate()
                self.mpv_process.wait(timeout=5)
                print("MPV stopped successfully")
            except subprocess.TimeoutExpired:
                self.mpv_process.kill()
                print("MPV killed forcefully")
            except Exception as e:
                print(f"Error stopping MPV: {e}")

            self.mpv_process = None
            self.is_playing = False

    def send_mpv_command(self, command):
        if not self.is_playing:
            print("MPV is not running, cannot send command")
            return None

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                sock.connect(self.config["mpv"]["socket_path"])
                sock.send((command + "\n").encode())
                return sock.recv(1024).decode().strip()
        except Exception as e:
            print(f"IPC error: {e}")
            return None

    def handle_play_command(self):
        print("Received play command")
        if self.prepare_playlist():
            return self.start_mpv_drm()
        return False

    def handle_stop_command(self):
        print("Received stop command")
        self.stop_mpv()
        return True

class MQTTClient:
    def __init__(self, controller):
        self.controller = controller
        self.client = mqtt.Client()
        self.client.on_message = self.on_message
        self.client.on_connect = self.on_connect

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            print("Connected to MQTT broker successfully")
            mqtt_cfg = self.controller.config["mqtt"]
            self.client.subscribe(mqtt_cfg["topic"])
            print(f"Subscribed to topic: {mqtt_cfg['topic']}")
        else:
            print(f"Failed to connect to MQTT broker with code: {rc}")

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            command = payload.get("command", "").lower()

            if command == "play":
                if self.controller.handle_play_command():
                    print("Playback started successfully")
                else:
                    print("Failed to start playback")

            elif command == "stop":
                self.controller.handle_stop_command()
                print("Playback stopped")

            elif command and self.controller.is_playing:
                # Отправляем команды только если MPV запущен
                if response := self.controller.send_mpv_command(command):
                    print(f"Executed: {command} → {response}")
            else:
                print(f"Ignoring command '{command}' - MPV not running or invalid command")

        except json.JSONDecodeError:
            print("Invalid JSON received")
        except Exception as e:
            print(f"Error processing message: {e}")

    def run(self):
        mqtt_cfg = self.controller.config["mqtt"]

        # Установка логина и пароля, если они указаны в конфиге
        if "username" in mqtt_cfg and "password" in mqtt_cfg:
            self.client.username_pw_set(
                mqtt_cfg["username"],
                mqtt_cfg["password"]
            )
            print(f"Using MQTT authentication for user: {mqtt_cfg['username']}")

        try:
            self.client.connect(mqtt_cfg["broker"], mqtt_cfg["port"])
            print(f"Connecting to MQTT at {mqtt_cfg['broker']}:{mqtt_cfg['port']}")
            self.client.loop_forever()
        except Exception as e:
            print(f"MQTT connection error: {e}")
            raise SystemExit(1)

if __name__ == "__main__":
    try:
        controller = MPVController()
        print("MPV MQTT Controller started. Waiting for commands...")
        print("Send 'play' command to start playback, 'stop' to stop")
        MQTTClient(controller).run()
    except KeyboardInterrupt:
        print("Shutting down...")
    except Exception as e:
        print(f"Fatal error: {e}")
    finally:
        if 'controller' in locals():
            controller.stop_mpv()
        SystemExit(0)