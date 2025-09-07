#!/usr/bin/python3
import os
import json
import re
import subprocess
import socket
import paho.mqtt.client as mqtt
import time
import threading
import select
from collections import deque

class MPVController:
    def __init__(self, config_path="./config.json"):
        self.config = self.load_config(config_path)
        self.mpv_process = None
        self.mpv_log_handle = None  # Add log file handle
        self.playlist = []
        self.is_playing = False
        self.current_state = {
            "state": "idle",
            "media_content_id": None,
            "media_title": None,
            "volume": 100,
            "position": 0,
            "duration": 0
        }
        self.previous_state = self.current_state.copy()
        self.ipc_socket = None
        self.observation_thread = None
        self.stop_observation = False
        self.state_changed = False
        self.last_publish_time = 0
        self.publish_interval = self.config["mqtt"].get("publish_interval", 1.0)  # секунды

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

    def get_playlist_path(self):
        """Возвращает полный путь к плейлисту в папке media_dir"""
        return os.path.join(self.config["mpv"]["media_dir"], self.config["mpv"]["playlist_file"])

    def prepare_playlist(self, force=False):
        """Создаёт новый плейлист, если force=True или файла нет"""
        playlist_path = self.get_playlist_path()
        if not force and os.path.exists(playlist_path):
            print(f"Using existing playlist: {playlist_path}")
            # Загружаем плейлист в память
            with open(playlist_path, "r") as f:
                self.playlist = [line.strip() for line in f if line.strip()]
            return True

        try:
            media_dir = self.config["mpv"]["media_dir"]
            if not os.path.exists(media_dir):
                raise FileNotFoundError(f"Media directory {media_dir} not found")

            # Получаем список файлов, соответствующих шаблону
            # Сначала обрабатываем корневую директорию
            root_files = []
            for filename in os.listdir(media_dir):
                file_path = os.path.join(media_dir, filename)
                if os.path.isfile(file_path) or (os.path.islink(file_path) and not os.path.isdir(file_path)):
                    if re.fullmatch(self.config["mpv"]["file_pattern"], filename):
                        root_files.append(file_path)

            # Затем рекурсивно обрабатываем поддиректории
            subdir_files = []
            visited_dirs = set()
            for root, dirs, filenames in os.walk(media_dir, followlinks=True):
                real_root = os.path.realpath(root)
                if real_root in visited_dirs:
                    # Удаляем поддиректории чтобы избежать циклов
                    dirs[:] = []
                    continue
                visited_dirs.add(real_root)

                # Пропускаем корневую директорию (уже обработали)
                if root == media_dir:
                    # Но все равно сортируем поддиректории для правильного порядка обхода
                    dirs.sort(key=self.natural_sort_key)
                    continue

                # Сортируем директории и файлы для детерминированного порядка
                dirs.sort(key=self.natural_sort_key)
                filenames.sort(key=self.natural_sort_key)

                for filename in filenames:
                    file_path = os.path.join(root, filename)
                    # Проверяем соответствие паттерну
                    if re.fullmatch(self.config["mpv"]["file_pattern"], filename):
                        subdir_files.append(file_path)

            # Объединяем списки: сначала корневая, затем поддиректории
            # Сортируем корневой список и список поддиректорий отдельно
            root_sorted = sorted(root_files, key=self.natural_sort_key)
            subdir_sorted = sorted(subdir_files, key=self.natural_sort_key)
            files = root_sorted + subdir_sorted

            if not files:
                print("No media files found matching the pattern")
                return False

            self.playlist = files

            with open(playlist_path, "w") as f:
                f.write("\n".join(self.playlist))

            print(f"Playlist created with {len(self.playlist)} files at {playlist_path}")
            return True

        except Exception as e:
            print(f"Playlist error: {e}")
            return False

    def setup_ipc_connection(self):
        """Устанавливает соединение с IPC сокетом MPV"""
        try:
            self.ipc_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.ipc_socket.connect(self.config["mpv"]["socket_path"])
            self.ipc_socket.setblocking(0)  # Неблокирующий режим
            print("Connected to MPV IPC socket")
            return True
        except Exception as e:
            print(f"Failed to connect to MPV IPC socket: {e}")
            return False

    def send_ipc_command(self, command):
        """Отправляет команду через IPC сокет"""
        if not self.ipc_socket:
            print("No IPC connection")
            return None

        try:
            self.ipc_socket.send((command + "\n").encode())
            return True
        except Exception as e:
            print(f"Failed to send IPC command: {e}")
            return False

    def setup_property_observation(self):
        """Настраивает наблюдение за свойствами MPV"""
        properties = [
            "pause",
            "path",
            "time-pos",
            "duration",
            "volume"
        ]

        for prop in properties:
            cmd = f'{{"command": ["observe_property", 1, "{prop}"]}}'
            if not self.send_ipc_command(cmd):
                print(f"Failed to observe property: {prop}")

        print("Property observation setup complete")

    def start_observation_thread(self, callback):
        """Запускает поток для наблюдения за изменениями свойств"""
        self.stop_observation = False
        self.observation_thread = threading.Thread(
            target=self.observe_properties,
            args=(callback,)
        )
        self.observation_thread.daemon = True
        self.observation_thread.start()
        print("Property observation thread started")

    def observe_properties(self, callback):
        """Поток для наблюдения за изменениями свойств MPV"""
        while not self.stop_observation and self.ipc_socket:
            try:
                # Используем select для проверки доступности данных
                ready = select.select([self.ipc_socket], [], [], 1.0)
                if ready[0]:
                    data = self.ipc_socket.recv(4096).decode()
                    if data:
                        for line in data.strip().split('\n'):
                            if line:
                                try:
                                    event = json.loads(line)
                                    self.process_event(event)
                                    # Помечаем, что состояние изменилось
                                    self.state_changed = True
                                except json.JSONDecodeError:
                                    print(f"Failed to parse JSON: {line}")
                elif not self.is_playing:
                    # Если MPV не воспроизводится, прекращаем наблюдение
                    break
            except Exception as e:
                print(f"Error in observation thread: {e}")
                time.sleep(1)  # Пауза перед повторной попыткой

        print("Property observation thread stopped")

    def process_event(self, event):
        """Обрабатывает события от MPV"""
        if "event" in event and event["event"] == "property-change":
            name = event.get("name")
            data = event.get("data")

            if name == "pause":
                new_state = "paused" if data else "playing"
                if self.current_state["state"] != new_state:
                    self.current_state["state"] = new_state
                    print(f"Playback state: {self.current_state['state']}")
            elif name == "path" and data:
                new_content_id = data
                new_title = os.path.basename(data)
                if (self.current_state["media_content_id"] != new_content_id or
                    self.current_state["media_title"] != new_title):
                    self.current_state["media_content_id"] = new_content_id
                    self.current_state["media_title"] = new_title
                    print(f"Now playing: {self.current_state['media_title']}")
            elif name == "time-pos" and data is not None:
                self.current_state["position"] = data
            elif name == "duration" and data is not None:
                self.current_state["duration"] = data
            elif name == "volume" and data is not None:
                self.current_state["volume"] = data

    def should_publish_state(self):
        """Проверяет, нужно ли публиковать состояние"""
        current_time = time.time()

        # Проверяем, изменилось ли состояние
        state_changed = (
            self.current_state["state"] != self.previous_state["state"] or
            self.current_state["media_content_id"] != self.previous_state["media_content_id"] or
            self.current_state["media_title"] != self.previous_state["media_title"] or
            abs(self.current_state["position"] - self.previous_state["position"]) > 0.5 or  # Позиция изменилась более чем на 0.5 сек
            abs(self.current_state["volume"] - self.previous_state["volume"]) > 1 or  # Громкость изменилась более чем на 1%
            current_time - self.last_publish_time >= self.publish_interval
        )

        return state_changed

    def update_previous_state(self):
        """Обновляет предыдущее состояние"""
        self.previous_state = self.current_state.copy()

    def start_mpv_drm(self, state_callback):
        if self.mpv_process and self.mpv_process.poll() is None:
            print("MPV is already running")
            return True

        playlist_path = self.get_playlist_path()
        if not os.path.exists(playlist_path):
            if not self.prepare_playlist(force=True):
                print("Failed to create playlist for playback")
                return False

        base_cmd = [
            "mpv",
            f"--input-ipc-server={self.config['mpv']['socket_path']}",
            "--vo=drm",
            "--audio-device=alsa/default",
            "--no-input-default-bindings",
            "--no-input-cursor",
            "--profile=sw-fast",
            f"--playlist={playlist_path}",
            "--loop-playlist"
        ]

        try:
            # Убедимся, что старый сокет удален
            if os.path.exists(self.config["mpv"]["socket_path"]):
                os.remove(self.config["mpv"]["socket_path"])

            # Handle log file configuration from mpv_log parameter
            log_config = self.config["mpv"].get("mpv_log", "mpv.log")
            log_file = None

            if not log_config:
                # If empty or None, use /dev/null
                log_file = "/dev/null"
            else:
                # Check if absolute path
                if os.path.isabs(log_config):
                    log_file = log_config
                else:
                    # Relative path - use media_dir as root
                    log_file = os.path.join(self.config["mpv"]["media_dir"], log_config)

            try:
                # Try to open log file
                self.mpv_log_handle = open(log_file, "a")
            except Exception as e:
                print(f"Failed to open log file '{log_file}': {e}. Redirecting to /dev/null")
                log_file = "/dev/null"
                try:
                    self.mpv_log_handle = open(log_file, "a")
                except Exception as e2:
                    print(f"Critical error opening /dev/null: {e2}. Logging disabled.")
                    self.mpv_log_handle = None

            self.mpv_process = subprocess.Popen(
                base_cmd,
                stdout=self.mpv_log_handle,
                stderr=self.mpv_log_handle,
                start_new_session=True
            )

            # Ждем, пока сокет станет доступен
            socket_wait_time = 0
            while not os.path.exists(self.config["mpv"]["socket_path"]) and socket_wait_time < 10:
                time.sleep(0.5)
                socket_wait_time += 0.5

            if not os.path.exists(self.config["mpv"]["socket_path"]):
                print("MPV socket not created after 10 seconds")
                return False

            # Настраиваем IPC соединение и наблюдение
            if self.setup_ipc_connection():
                self.is_playing = True
                self.current_state["state"] = "playing"
                self.current_state["media_content_id"] = self.playlist[0] if self.playlist else None
                self.current_state["media_title"] = os.path.basename(self.playlist[0]) if self.playlist else None

                self.setup_property_observation()
                self.start_observation_thread(state_callback)

                print("MPV started successfully with property observation")
                return True
            else:
                print("Failed to setup IPC connection")
                return False

        except Exception as e:
            print(f"MPV start failed: {e}")
            self.is_playing = False
            self.current_state["state"] = "idle"
            return False

    def stop_mpv(self):
        # Останавливаем наблюдение
        self.stop_observation = True

        if self.observation_thread and self.observation_thread.is_alive():
            self.observation_thread.join(timeout=2.0)

        # Закрываем IPC соединение
        if self.ipc_socket:
            try:
                self.ipc_socket.close()
            except:
                pass
            self.ipc_socket = None

        # Останавливаем процесс MPV
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
            self.current_state["state"] = "idle"
            self.current_state["media_content_id"] = None
            self.current_state["media_title"] = None
            self.current_state["position"] = 0
            self.current_state["duration"] = 0

            # Close log file if open
            if self.mpv_log_handle:
                self.mpv_log_handle.close()
                self.mpv_log_handle = None

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

    def handle_play_command(self, state_callback):
        print("Received play command")
        if self.prepare_playlist():
            return self.start_mpv_drm(state_callback)
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

        # Получаем полные имена топиков из конфига
        mqtt_cfg = self.controller.config["mqtt"]
        self.control_topic = f"{mqtt_cfg['base_topic']}{mqtt_cfg['control_topic']}"
        self.state_topic = f"{mqtt_cfg['base_topic']}{mqtt_cfg['state_topic']}"

        # Таймер для периодической проверки состояния
        self.publish_timer = None

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            print("Connected to MQTT broker successfully")
            # Подписываемся на топик управления
            self.client.subscribe(self.control_topic)
            print(f"Subscribed to control topic: {self.control_topic}")
            print(f"State will be published to: {self.state_topic}")

            # Публикуем начальное состояние при подключении
            self.publish_state(self.controller.current_state)

            # Запускаем таймер для периодической проверки состояния
            self.start_publish_timer()
        else:
            print(f"Failed to connect to MQTT broker with code: {rc}")

    def start_publish_timer(self):
        """Запускает таймер для периодической публикации состояния"""
        if self.publish_timer:
            self.publish_timer.cancel()

        self.publish_timer = threading.Timer(0.5, self.check_and_publish_state)  # Проверяем каждые 0.5 сек
        self.publish_timer.daemon = True
        self.publish_timer.start()

    def check_and_publish_state(self):
        """Проверяет, нужно ли публиковать состояние, и публикует если нужно"""
        try:
            if self.controller.is_playing and self.controller.should_publish_state():
                self.publish_state(self.controller.current_state)
                self.controller.update_previous_state()
                self.controller.last_publish_time = time.time()
        except Exception as e:
            print(f"Error in publish timer: {e}")

        # Перезапускаем таймер
        self.start_publish_timer()

    def publish_state(self, state):
        """Публикует текущее состояние проигрывателя"""
        try:
            self.client.publish(
                self.state_topic,
                json.dumps(state),
                retain=True
            )
            # print(f"State published to {self.state_topic}: {state['state']}, position: {state.get('position', 0):.1f}s")
        except Exception as e:
            print(f"Error publishing state: {e}")

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            command = payload.get("command", "").lower()

            if command == "play":
                if self.controller.handle_play_command(self.publish_state):
                    print("Playback started successfully")
                    # Принудительно публикуем состояние после запуска
                    self.publish_state(self.controller.current_state)
                    self.controller.update_previous_state()
                    self.controller.last_publish_time = time.time()
                else:
                    print("Failed to start playback")

            elif command == "stop":
                self.controller.handle_stop_command()
                print("Playback stopped")
                # Принудительно публикуем состояние после остановки
                self.publish_state(self.controller.current_state)
                self.controller.update_previous_state()
                self.controller.last_publish_time = time.time()

            elif command == "clear-playlist":
                print("Received clear-playlist command")
                self.controller.stop_mpv()
                playlist_path = self.controller.get_playlist_path()
                if os.path.exists(playlist_path):
                    try:
                        os.remove(playlist_path)
                        print(f"Old playlist removed: {playlist_path}")
                    except Exception as e:
                        print(f"Failed to remove playlist: {e}")
                if self.controller.prepare_playlist(force=True):
                    if self.controller.start_mpv_drm(self.publish_state):
                        print("Playlist cleared and playback restarted")
                        self.publish_state(self.controller.current_state)
                        self.controller.update_previous_state()
                        self.controller.last_publish_time = time.time()
                    else:
                        print("Failed to restart playback after clearing playlist")
                else:
                    print("Failed to prepare new playlist")

            elif command and self.controller.is_playing:
                # Отправляем команды только если MPV запущен
                if response := self.controller.send_mpv_command(command):
                    print(f"Executed: {command} → {response}")
                    # После команды небольшая задержка для обновления состояния
                    time.sleep(0.1)
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

            # Запускаем фоновый цикл MQTT
            self.client.loop_forever()

        except Exception as e:
            print(f"MQTT connection error: {e}")
            raise SystemExit(1)
        except KeyboardInterrupt:
            print("MQTT loop interrupted")
        finally:
            if self.publish_timer:
                self.publish_timer.cancel()

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