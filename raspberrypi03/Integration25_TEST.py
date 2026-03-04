import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk
import subprocess
import re  # Import for IP validation
import os
import json
import threading
import time
import paho.mqtt.client as mqtt
from playsound import playsound
from collections import deque
import mariadb
import RPi.GPIO as GPIO

from imagerecognition6 import EmbeddedHailoViewer
from Translation import translations, audio_map

LOGO_OPTIONS = {
    "Giant": {
        "key": "giant",
        "path": "/home/pi/hailo-rpi5-examples/images/Giant Logo.png"
    },
    "Prime": {
        "key": "prime",
        "path": "/home/pi/hailo-rpi5-examples/images/Prime Logo.png"
    }
}


# ---------- CONSTANTS / CONFIG ----------

DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "root",
    "database": "Anti_Theftdb"
}

RELAY_PIN = 17
DEBOUNCE_COUNT = 2

DEFAULT_MQTT_IP = "192.168.5.2"
DEFAULT_SHUTDOWN_PASSWORD = "1234"

VOLUME_STEPS = [0, 20, 30, 40, 50, 60, 70, 80, 90, 100]


class GradientPlaceholder:
    def __init__(self, master):
        self.master = master
        self.color1 = "white"
        self.color2 = "white"

    def _draw_gradient(self):
        pass


class PageController(tk.Tk):

    def get_audio_file(self, alert_key):
        lang = self.current_language.get()

        try:
            return audio_map[alert_key].get(lang, audio_map[alert_key]["en"])
        except Exception:
            return None

    def play_staff_assist_audio(self):
        # 🚫 Do not allow staff audio during mismatch
        if self.mismatch_active:
            return

        def audio_loop():
            while self.staff_assist_active and not self.mismatch_active:
                try:
                    self.play_audio("staff_assistance")
                except Exception as e:
                    print("Staff audio error:", e)
                time.sleep(0.2)

        self.staff_audio_thread = threading.Thread(
            target=audio_loop,
            daemon=True
        )
        self.staff_audio_thread.start()

    def play_mismatch_audio_loop(self):
        def audio_loop():
            while self.mismatch_active:
                try:
                    self.play_audio("mismatch")
                except Exception as e:
                    print("Mismatch audio error:", e)
                time.sleep(0.2)
        self.mismatch_audio_thread = threading.Thread(
            target=audio_loop,
            daemon=True
        )
        self.mismatch_audio_thread.start()

    def adjust_speaker_volume(self, slider_value):
        slider = int(slider_value)

        # Snap to nearest allowed discrete step
        nearest = min(VOLUME_STEPS, key=lambda x: abs(x - slider))

        # Update slider visually (prevents in-between values)
        if hasattr(self, "temp_volume") and self.temp_volume.get() != nearest:
            self.temp_volume.set(nearest)

        try:
            subprocess.run(
                ["amixer", "sset", "Master", f"{nearest}%"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            print(f"[AUDIO] Volume set → {nearest}%")
        except Exception as e:
            print("[AUDIO] Failed to set volume:", e)


    def show_activity_log(self):
        """Fetches logs from MariaDB and displays them in a themed popup."""
        # Step 1: Fetch data from MariaDB
        try:
            conn = mariadb.connect(**DB_CONFIG)
            cursor = conn.cursor()
            # Selection query based on your mariadb.py/Integration13 logic
            cursor.execute("SELECT timestamp, description FROM activity_log ORDER BY timestamp DESC")
            records = cursor.fetchall()
            conn.close()
        except mariadb.Error as err:
            self.show_message_box(f"Database Error: {err}")
            return

        # Step 2: UI Setup using current theme
        colors = self.get_theme_colors()
        log_window = tk.Toplevel(self)
        log_window.title(self.get_text("activity_log"))
        log_window.geometry("1000x600")
        log_window.configure(bg="black") # Border color
        log_window.overrideredirect(True)
        log_window.grab_set()
        
        self.center_popup(log_window, 1000, 600)

        # Inner frame for consistent border look
        inner_frame = tk.Frame(log_window, bg=colors["main_bg"], bd=2)
        inner_frame.pack(fill="both", expand=True, padx=2, pady=2)

        # Title Bar (Matching your system's style)
        title_bar = tk.Frame(inner_frame, bg="#2E7D32", height=60)
        title_bar.pack(fill="x")
        tk.Label(
            title_bar, 
            text=self.get_text("activity_log").replace("✍️ ", ""), 
            font=("Arial", 22, "bold"), 
            bg="#2E7D32", 
            fg="white"
        ).pack(pady=10)

        # Table Styling (Treeview)
        style = ttk.Style()
        style.theme_use("default")
        style.configure("Treeview", 
                        background=colors["input_bg"], 
                        foreground=colors["input_fg"], 
                        fieldbackground=colors["input_bg"],
                        rowheight=30)
        style.map("Treeview", background=[('selected', colors["highlight"])])
        
        # Scrollbar
        tree_frame = tk.Frame(inner_frame, bg=colors["main_bg"])
        tree_frame.pack(fill="both", expand=True, padx=20, pady=20)
        
        tree_scroll = tk.Scrollbar(tree_frame)
        tree_scroll.pack(side="right", fill="y")

        # Table Creation
        columns = ("Time", "Description")
        tree = ttk.Treeview(tree_frame, columns=columns, show="headings", yscrollcommand=tree_scroll.set)
        tree_scroll.config(command=tree.yview)

        tree.heading("Time", text=self.get_text("time"))
        tree.heading("Description", text=self.get_text("description"))
        tree.column("Time", width=250, anchor="center")
        tree.column("Description", width=650, anchor="w")

        # Insert Data
        for timestamp, desc in records:
            tree.insert("", "end", values=(timestamp, desc))

        tree.pack(fill="both", expand=True)

        # Close/OK Button
        tk.Button(
            inner_frame, 
            text=self.get_text("ok"), 
            command=log_window.destroy,
            bg=colors["highlight"], 
            fg="white", 
            font=("Arial", 14, "bold"),
            width=12, 
            height=2,
            cursor="hand2"
        ).pack(pady=15)

    def log_to_activity_log(self, description):
        try:
            conn = mariadb.connect(**DB_CONFIG)
            cursor = conn.cursor()
            cursor.execute("INSERT INTO activity_log (timestamp, description) VALUES (NOW(), ?)", (description,))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Logging Error: {e}")
        

    """
    Main application controller managing the window and page transitions.
    Includes state management for Settings (Theme, Volume, Language, IP, Password)
    and the counting / alert logic integrated from Integration13.
    """
    # ======================================================
    # PAGE NAVIGATION (MISSING METHOD – REQUIRED)
    # ======================================================
    def show_page(self, page_class):
        """
        Safely switches between pages without changing layout.
        This method existed in Integration13 and is REQUIRED.
        """
        # Hide current pages
        for page in self.pages.values():
            page.pack_forget()

        # Create page if not already created
        if page_class not in self.pages:
            page = page_class(self.container, self)
            self.pages[page_class] = page
        else:
            page = self.pages[page_class]

        # Show page
        page.pack(fill="both", expand=True)

        # Hide top-left icons on CounterPage only
        if page_class == CounterPage:
            self.toggle_top_left_icons(False)
        else:
            self.toggle_top_left_icons(True)

        if page_class == CounterPage:
            self.counter_mode_active = True
            if self.mqtt_client:
                self.mqtt_client.publish("counter/control", "START")
        else:
            self.counter_mode_active = False
            if self.mqtt_client:
                self.mqtt_client.publish("counter/control", "STOP")

    # ======================================================
    # THEME REFRESH (FIX DARK MODE)
    # ======================================================
    def refresh_page_theme(self, page):
        """
        Recursively updates background & foreground colors
        for all widgets on a page.
        """
        colors = self.get_theme_colors()

        def apply(widget):
            try:
                if isinstance(widget, (tk.Frame, tk.LabelFrame)):
                    widget.config(bg=colors["main_bg"])
                elif isinstance(widget, tk.Label):
                    widget.config(
                        bg=colors["main_bg"],
                        fg=colors["fg_color"]
                    )
                elif isinstance(widget, tk.Button):
                    widget.config(
                        bg=colors["top_bg"],
                        fg=colors["fg_color"],
                        activebackground=colors["active_bg"]
                    )
                elif isinstance(widget, tk.Scale):
                    widget.config(
                        bg=colors["main_bg"],
                        fg=colors["fg_color"],
                        troughcolor="#555555" if self.current_theme.get() == "dark" else "#dddddd"
                    )
            except Exception:
                pass

            for child in widget.winfo_children():
                apply(child)

        apply(page)
    
    def init_flash_screen(self):
        if self.flash_screen and self.flash_screen.winfo_exists():
            return

        self.flash_screen = tk.Toplevel(self)
        self.flash_screen.configure(bg="white")
        self.flash_screen.attributes("-topmost", True)
        self.flash_screen.geometry(
            f"{self.flash_screen.winfo_screenwidth()}x{self.flash_screen.winfo_screenheight()}"
        )
        self.flash_screen.overrideredirect(True)
        self.flash_screen.withdraw()

        flash_frame = tk.Frame(self.flash_screen, bg="white")
        flash_frame.pack(expand=True, fill="both", padx=20, pady=20)

        self.flash_image_label = tk.Label(flash_frame, bg="white")
        self.flash_image_label.pack(pady=(100, 20))

        self.flash_label = tk.Label(
            flash_frame,
            bg="white",
            font=("Times New Roman", 48, "bold"),
            fg="white"
        )
        self.flash_label.pack()

        self.flash_images = {
            "barcode_detected": ImageTk.PhotoImage(
                Image.open("/home/pi/hailo-rpi5-examples/images/barcode_detected.jpeg")
                .resize((450, 500))
            ),
            "barcode_blocked": ImageTk.PhotoImage(
                Image.open("/home/pi/hailo-rpi5-examples/images/barcode_blocked.jpeg")
                .resize((450, 500))
            ),
            "camera_blocked": ImageTk.PhotoImage(
                Image.open("/home/pi/hailo-rpi5-examples/images/camera_blocked.jpeg")
                .resize((450, 500))
            ),
        }
    
    def flash_event(self, text_key):
        if not self.flash_screen or not self.flash_screen.winfo_exists():
            return

        now = time.time()
        with self.flash_lock:
            if now - self.last_flash_time < self.flash_cooldown:
                return
            self.last_flash_time = now

        # --- ADD THIS DATABASE LOGGING SECTION ---
        db_alert = ""
        if text_key == "camera_blocked":
            db_alert = self.get_text("camera_block_alert") # Should be "⚠️ Camera Block Detected"
        elif text_key == "barcode_blocked":
            db_alert = "⚠️ Barcode Blocked Detected"

        if db_alert:
            try:
                conn = mariadb.connect(**DB_CONFIG)
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO activity_log (timestamp, description) VALUES (NOW(), ?)",
                    (db_alert,)
                )
                conn.commit()
                conn.close()
            except mariadb.Error as err:
                print(f"Database error in flash_event: {err}")
        # -----------------------------------------

        if text_key == "barcode_detected":
            self._flash_green_tick(text_key)
            self.play_audio("barcode_detected")

        elif text_key == "barcode_blocked":
            self._flash_red_screen(text_key)
            self.play_audio("barcode_blocked")

        elif text_key == "camera_blocked":
            self._flash_red_screen(text_key)
            self.play_audio("camera_blocked")


    def _flash_green_tick(self, key):
        img = self.flash_images.get(key)
        if not img:
            return

        self.flash_screen.configure(bg="#00c853")
        self.flash_image_label.config(image=img, bg="#00c853")
        self.flash_image_label.image = img
        self.flash_label.config(
            text="✓ Barcode Detected",
            fg="white",
            bg="#00c853"
        )

        self.flash_screen.deiconify()
        self.flash_screen.after(1500, self.flash_screen.withdraw)


    def _flash_red_screen(self, key):
        img = self.flash_images.get(key)
        if not img:
            return

        # --- LOGGING LOGIC ---
        if key == "mismatch_alert":
            # 1. Log the Header
            self.log_to_activity_log(self.get_text("mismatch_alert"))
            
            # 2. Get the actual integers and format the string
            # This matches {dome} and {ribbon} in your Translation.py
            detailed_msg = self.get_text("mismatch_message").format(
                dome=self.dome_count, 
                ribbon=self.ribbon_count
            )
            
            # 3. Log the message: "Mismatch detected: Object Count (1) does not match..."
            self.log_to_activity_log(detailed_msg)
            text = self.get_text("mismatch_alert")
            
        elif key == "camera_blocked":
            text = "✗ Camera Blocked"
            self.log_to_activity_log(self.get_text("camera_block_alert"))
            
        elif key == "barcode_blocked":
            text = "✗ Barcode Blocked"
            self.log_to_activity_log("⚠️ Barcode Blocked Detected")
        else:
            text = "✗ BLOCKED"

        # --- VISUAL UI LOGIC ---
        self.flash_screen.configure(bg="#d50000")
        self.flash_image_label.config(image=img, bg="#d50000")
        self.flash_label.config(text=text, fg="white", bg="#d50000")
        self.flash_screen.deiconify()
        self.flash_screen.after(1500, self.flash_screen.withdraw)

    SETTINGS_FILE = "settings.json"

    def __init__(self):
        super().__init__()
        self.title("Giant Supermarket System - Clean GUI")
        self.attributes("-fullscreen", True)
        self.is_fullscreen = True
        self.configure(bg="white")

        # 🔒 Dome freeze after payment reset
        self.dome_freeze_until = 0

        self.update_idletasks()
        self.screen_w = self.winfo_screenwidth()
        self.screen_h = self.winfo_screenheight()

        print(f"[SCREEN] {self.screen_w}x{self.screen_h}")
        
        # 🖱️ HIDE MOUSE CURSOR
        self.config(cursor="none")

        self.mismatch_direction = None   # "dome_gt" or "ribbon_gt"
        self.mismatch_baseline_time = None

        self.staff_assist_active = False
        self.staff_audio_thread = None

        self.mismatch_active = False
        self.mismatch_audio_thread = None
        self.mismatch_acknowledged = False

        self.password_popup_active = False

        self.staff_auth_until = 0
        self.STAFF_AUTH_GRACE = 10

        self.flash_screen = None
        self.flash_lock = threading.Lock()
        self.last_flash_time = 0
        self.flash_cooldown = 1.3
        self.current_flash_key = None

        self.counter_mode_active = False

        # ---------- AUDIO STATE (FIX OVERLAP) ----------
        self.audio_lock = threading.Lock()
        self.audio_playing = False
        self.last_audio_played = None
        self.last_audio_time = 0
        self.audio_cooldown = 0.5  # seconds

        # =====================
        # COUNTER STATE & LOGIC STATE
        # =====================
        self.dome_count = 0         # Object count (dome camera)
        self.ribbon_count = 0       # Summary / scanner count (ribbon camera)
        self.camera_block_count = 0
        self.mismatch_count = 0
        self.last_logged_dome = 0
        self.last_logged_ribbon = 0


        self.last_mismatch_time = None
        self.mismatch_grace = 6  # seconds (used originally – kept for reference)

        # New logic state (from Integration13)
        self.number_lock = threading.Lock()

        self.recent_dome_counts = deque(maxlen=3)
        self.last_dome_increment_time = 0
        self.dome_debounce_interval = 1  # seconds

        self.last_ribbon_adjust_time = 0
        self.ribbon_adjust_debounce = 1 # seconds

        self.last_mismatch_state = None
        self.mismatch_start_time = None
        self.last_logged_mismatch_dome_count = 0

        self.counter_page_ref = None  # reference to CounterPage
        self.mqtt_client = None

        # --- STATE AND THEME VARIABLES ---
        self.current_language = tk.StringVar(value="en")
        self.current_theme = tk.StringVar(value="light")
        self.volume = tk.IntVar(value=50)
        self.mqtt_ip = tk.StringVar()
        self.shutdown_password = tk.StringVar()
        self.current_logo = tk.StringVar(value="giant")

        # Load settings (including shutdown password, language, theme, volume, MQTT IP)
        self.load_settings()

        self.adjust_speaker_volume(self.volume.get())

        # Ensure defaults if settings missing
        if not self.mqtt_ip.get():
            self.mqtt_ip.set(DEFAULT_MQTT_IP)

        if not self.shutdown_password.get():
            self.shutdown_password.set(DEFAULT_SHUTDOWN_PASSWORD)

        # Simulated properties to allow the user's apply_theme logic to run without error
        self.bg_gradient_start = "white"
        self.bg_gradient_end = "#aaffaa"
        self.bg_color = "white"
        self.grid_frame_bg = "white"
        self.text_fg = "black"
        self.btn_bg = "#e0e0e0"
        self.fg_color = "black"
        self.btn_active_bg = "#cccccc"
        self.top_bar_bg = "white"

        self.gradient_bg = GradientPlaceholder(self)
        self.border_frame = tk.Frame(self, bg=self.bg_color)
        self.grid_frame = tk.Frame(self.border_frame, bg=self.grid_frame_bg)
        self.status_label = tk.Label(self, text="", bg=self.bg_color, fg=self.fg_color)

        # --------------------------------------------------
        # TOP BAR (Height standardized to 140)
        # --------------------------------------------------
        self.top_bar = tk.Frame(self, bg="white", height=140)
        self.top_bar.pack(side="top", fill="x")
        self.top_bar.pack_propagate(False)

        # Left vertical icons container
        self.left_icon_frame = tk.Frame(self.top_bar, bg="white")
        self.left_icon_frame.pack(side=tk.LEFT, padx=20, pady=5)

        # Settings icon (Top of the stack)
        self.settings_btn = tk.Button(
            self.left_icon_frame,
            text="⚙",
            font=("Arial", 22),
            bg="white",
            bd=0,
            cursor="hand2",
            command=self.show_settings_popup
        )
        self.settings_btn.grid(row=0, column=0, pady=(15, 5))

        # About icon (Below settings)
        self.about_btn = tk.Button(
            self.left_icon_frame,
            text="ℹ️",
            font=("Arial", 22),
            bg="white",
            bd=0,
            cursor="hand2",
            command=self.show_about_popup
        )
        self.about_btn.grid(row=1, column=0, pady=(5, 15))

        # Shutdown icon (right side)
        shutdown_path = os.path.expanduser("/home/pi/hailo-rpi5-examples/images/pb2.png")
        self.shutdown_btn = tk.Button(
            self.top_bar,
            text="⏻",
            font=("Arial", 22),
            bg="white",
            bd=0,
            cursor="hand2",
            command=self.prompt_for_shutdown_password
        )
        # ---------- DISPLAY MODE TOGGLE (PASSWORD PROTECTED) ----------
        self.is_fullscreen = True
        self.windowed_size = (1280, 720)

        self.display_btn = tk.Button(
            self.top_bar,
            text="🖥️",
            font=("Arial", 22),
            bg="white",
            bd=0,
            cursor="hand2",
            command=self.toggle_fullscreen_with_password
        )
        self.display_btn.pack(side=tk.RIGHT, padx=10, pady=(20, 0))

        if os.path.exists(shutdown_path):
            shutdown_img = Image.open(shutdown_path)
            shutdown_img = shutdown_img.resize((40, 40))
            self.shutdown_img_tk = ImageTk.PhotoImage(shutdown_img)
            self.shutdown_btn.config(image=self.shutdown_img_tk)
            self.shutdown_btn.image = self.shutdown_img_tk
        else:
            self.shutdown_btn.config(text="Shutdown")
        self.shutdown_btn.pack(side=tk.RIGHT, padx=20, pady=(20, 0))

        # --------------------------------------------------
        # PAGE CONTAINER
        # --------------------------------------------------
        self.container = tk.Frame(self, bg="white")
        self.container.pack(fill="both", expand=True)

        self.bind("<FocusIn>", lambda e: self.bring_gui_to_front())

        self.pages = {}
        self.show_page(HomePage)

        self.after(50, self._force_fullscreen_startup)

        # GPIO for visual alert (relay)
        self.relay_active = False
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(RELAY_PIN, GPIO.OUT)
            GPIO.output(RELAY_PIN, GPIO.LOW)
            print("GPIO relay initialized.")
        except Exception as e:
            print(f"Warning: GPIO setup failed ({e}). Visual alerts disabled.")

        # Apply initial theme
        self.apply_theme()

        self.after(200, self.check_mismatch_timer)

    def toggle_fullscreen_with_password(self):
        def do_toggle():
            self.update_idletasks()

            if self.is_fullscreen:
                self.attributes("-fullscreen", False)
                self.geometry("1280x800+0+0")
                self.resizable(False, False)
                self.is_fullscreen = False
                self.display_btn.config(text="🗖")
            else:
                self.geometry("")
                self.attributes("-fullscreen", True)
                self.is_fullscreen = True
                self.display_btn.config(text="🖥️")

            # 🔥 ADD THESE 3 LINES ONLY
            self.after(200, self.force_layout_refresh)
            self.after(350, self.bring_gui_to_front)
            self.after(700, self.bring_gui_to_front)

        self.prompt_password(
            title=self.get_text("confirm"),
            message=self.get_text("enter_password"),
            on_success=do_toggle
        )


    # TOP LEFT ICON TOGGLE LOGIC
    # ======================================================
    def toggle_top_left_icons(self, visible):
        """Shows or hides the Settings (⚙) and About (ℹ️) buttons."""
        if visible:
            # Show icons using grid (as they are normally configured)
            self.settings_btn.grid(row=0, column=0, pady=(15, 5))
            self.about_btn.grid(row=1, column=0, pady=(5, 15))
        else:
            # Hide icons using grid_forget
            self.settings_btn.grid_forget()
            self.about_btn.grid_forget()

    # THEME AND TRANSLATION LOGIC
    # ======================================================
    def get_theme_colors(self):
        """Returns the consistent color scheme based on the current theme."""
        is_dark = self.current_theme.get() == "dark"

        DARK_GREY = "#2e2e2e"

        return {
            "main_bg": DARK_GREY if is_dark else "white",
            "top_bg": DARK_GREY if is_dark else "white",
            "fg_color": "white" if is_dark else "black",
            "active_bg": "#555555" if is_dark else "#d6f5d6",
            "input_bg": "#444444" if is_dark else "white",
            "input_fg": "white" if is_dark else "black",
            "highlight": "#6a906a" if is_dark else "#4CAF50"  # Green highlight
        }

    def apply_theme(self):
        colors = self.get_theme_colors()

        # Simulate changes to custom elements (to avoid errors)
        self.gradient_bg.color1 = self.bg_gradient_start
        self.gradient_bg.color2 = self.bg_gradient_end
        self.gradient_bg._draw_gradient()

        # Update core Tkinter colors
        self.config(bg=colors["main_bg"])
        self.top_bar.config(bg=colors["top_bg"])
        self.left_icon_frame.config(bg=colors["top_bg"])
        self.container.config(bg=colors["main_bg"])

        # Update top bar buttons
        self.settings_btn.config(
            bg=colors["top_bg"],
            fg=colors["fg_color"],
            activebackground=colors["active_bg"]
        )
        self.about_btn.config(
            bg=colors["top_bg"],
            fg=colors["fg_color"],
            activebackground=colors["active_bg"]
        )
        self.shutdown_btn.config(
            bg=colors["top_bg"],
            fg="red" if self.current_theme.get() == "dark" else "black",
            activebackground="#fddede" if self.current_theme.get() == "dark" else colors["active_bg"]
        )

        # Re-create the current page to apply theme correctly
        current_page_class = next(
            (cls for cls, page in self.pages.items() if page.winfo_ismapped()),
            HomePage
        )
        # Refresh all existing pages instead of recreating them
        for page in self.pages.values():
            self.refresh_page_theme(page)

        if self.flash_screen and self.flash_screen.winfo_exists():
            bg = self.get_theme_colors()["main_bg"]
            self.flash_screen.configure(bg=bg)

    def get_text(self, key):
        """Retrieves translated text."""
        return translations.get(self.current_language.get(), translations["en"]).get(key, key)

    def load_settings(self):
        """Load all persistent settings from JSON file if exists"""
        try:
            if os.path.exists(self.SETTINGS_FILE):
                with open(self.SETTINGS_FILE, "r") as f:
                    data = json.load(f)

                self.shutdown_password.set(
                    data.get("shutdown_password", DEFAULT_SHUTDOWN_PASSWORD)
                )
                self.current_language.set(
                    data.get("language", self.current_language.get())
                )
                self.current_theme.set(
                    data.get("theme", self.current_theme.get())
                )
                self.current_logo.set(
                    data.get("logo", self.current_logo.get())
                )
                self.volume.set(
                    data.get("volume", self.volume.get())
                )
                self.mqtt_ip.set(
                    data.get("mqtt_ip", self.mqtt_ip.get())
                )
            else:
                # File not found – set sane defaults
                self.shutdown_password.set(DEFAULT_SHUTDOWN_PASSWORD)
                if not self.mqtt_ip.get():
                    self.mqtt_ip.set(DEFAULT_MQTT_IP)
        except Exception as e:
            print(f"Error loading settings: {e}")
            self.shutdown_password.set(DEFAULT_SHUTDOWN_PASSWORD)
            if not self.mqtt_ip.get():
                self.mqtt_ip.set(DEFAULT_MQTT_IP)

    def save_settings_to_file(self):
        """Store settings permanently to JSON"""
        data = {
            "shutdown_password": self.shutdown_password.get(),
            "language": self.current_language.get(),
            "theme": self.current_theme.get(),
            "volume": self.volume.get(),
            "mqtt_ip": self.mqtt_ip.get(),
            "logo": self.current_logo.get()
        }
        try:
            with open(self.SETTINGS_FILE, "w") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            print(f"Error saving settings: {e}")

    # ======================================================
    # POPUP FUNCTIONALITY
    # ======================================================

    # MODIFIED: Added optional on_ok_callback
    def show_message_box(self, message, on_ok_callback=None):
        colors = self.get_theme_colors()
        msg_box = tk.Toplevel(self)
        # The size now refers to the total size including the border
        msg_box.geometry("300x170")
        msg_box.overrideredirect(True)
        msg_box.grab_set()
        msg_box.focus_set()

        # Custom destroy handler to run the callback
        def handle_destroy():
            msg_box.destroy()
            if on_ok_callback:
                on_ok_callback()

        # === BORDER BACKGROUND ===
        # Set the Toplevel's background to the border color (black)
        border_color = "black"
        msg_box.configure(bg=border_color)

        # Define the border thickness
        border_thickness = 2

        # === INNER CONTENT FRAME ===
        # This frame will hold all content and act as the main window background
        bg = colors["top_bg"]
        inner_frame = tk.Frame(msg_box, bg=bg)
        # Pack the inner frame with padding equal to the border thickness
        inner_frame.pack(fill="both", expand=True,
                         padx=border_thickness,
                         pady=border_thickness)

        # === CUSTOM TITLE BAR ===
        # *** Parent is inner_frame ***
        title_bar = tk.Frame(inner_frame, bg="#2E7D32", height=40)
        title_bar.pack(fill="x")

        tk.Label(
            title_bar,
            text=self.get_text("alert_title") if "alert_title" in translations[self.current_language.get()] else "Alert",
            font=("Arial", 14, "bold"),
            fg="white",
            bg="#2E7D32"
        ).pack(pady=5)

        # === MAIN CONTENT FRAME ===
        # *** Parent is inner_frame ***
        frame = tk.Frame(inner_frame, bg=bg)
        frame.pack(fill="both", expand=True)

        tk.Label(
            frame,
            text=message,
            font=("Arial", 12),
            bg=bg,
            fg=colors["fg_color"],
            wraplength=260
        ).pack(pady=20)

        tk.Button(
            frame,
            text=self.get_text("ok"),
            command=handle_destroy,  # <--- CALLS CUSTOM HANDLER
            font=("Arial", 12, "bold"),
            bg=colors["active_bg"],
            fg=colors["fg_color"],
            bd=1,
            height=2,
            cursor="hand2"
        ).pack(pady=10)

        # === CENTER POPUP ===
        msg_box.update_idletasks()
        self.center_popup(msg_box, msg_box.winfo_width(), msg_box.winfo_height())

    def perform_shutdown(self):
        """Final action before shutting down the application."""
        print(self.get_text("system_shutting_down"))
        # This is where the actual OS shutdown command would be called in a real system.
        self.destroy()  # Close the Tkinter window

    def prompt_for_shutdown_password(self):
        self.prompt_password(
            title=self.get_text("confirm"),
            message=self.get_text("enter_password"),
            on_success=self.perform_shutdown
        )

    def _force_fullscreen_startup(self):
        # Let Tk finish creating widgets
        self.update_idletasks()

        # Force full screen size explicitly (Raspberry Pi fix)
        w = self.winfo_screenwidth()
        h = self.winfo_screenheight()
        self.geometry(f"{w}x{h}+0+0")

        # Re-apply fullscreen AFTER geometry exists
        self.attributes("-fullscreen", True)

        # Force layout recalculation
        self.force_layout_refresh()

        # Bring window to front (Hailo / RTSP windows like to steal focus)
        self.bring_gui_to_front()
        self.after(200, self.bring_gui_to_front)
        self.after(600, self.bring_gui_to_front)

    def force_layout_refresh(self):
        """
        Forces Tk to recalculate geometry after fullscreen changes.
        Fixes half-screen / white margin issues on Raspberry Pi.
        """
        self.update_idletasks()
        self.update()

        for child in self.winfo_children():
            child.update_idletasks()

    def prompt_password(self, title, message, on_success):
        if self.password_popup_active:
                return  # 🚫 Do not stack password dialogs

        self.password_popup_active = True

        colors = self.get_theme_colors()

        popup = tk.Toplevel(self)
        popup_width = 600
        popup_height = 720  # Taller to fit numpad nicely

        popup.overrideredirect(True)
        popup.configure(bg="black")
        self.center_popup(popup, popup_width, popup_height)
        popup.grab_set()
        popup.focus_set()

        # Theme-aware styling
        panel_bg = colors["top_bg"]
        content_bg = colors["input_bg"]
        text_color = colors["fg_color"]

        # Main Panel
        panel = tk.Frame(popup, bg=panel_bg, bd=2, relief="solid")
        panel.pack(fill="both", expand=True)

        # Title Bar
        title_bar = tk.Frame(panel, bg="#2E7D32", height=60)
        title_bar.pack(fill="x")
        tk.Label(
            title_bar,
            text=title,
            font=("Arial", 20, "bold"),
            fg="white",
            bg="#2E7D32"
        ).pack(pady=10)

        # Content Frame
        content = tk.Frame(panel, bg=content_bg, padx=20, pady=20)
        content.pack(fill="both", expand=True)

        password_var = tk.StringVar()

        tk.Label(
            content,
            text=message,
            font=("Arial", 14, "bold"),
            bg=content_bg,
            fg=text_color
        ).pack(pady=10)

        # Password display
        password_display = tk.Entry(
            content,
            textvariable=password_var,
            font=("Arial", 24),
            width=12,
            justify='center',
            show="*",
            bg=colors["input_bg"],
            fg=colors["input_fg"]
        )
        password_display.pack(pady=10)
        password_display.focus_set()

        # --- NUMPAD ---
        numpad_frame = tk.Frame(content, bg=content_bg)
        numpad_frame.pack(pady=10)

        def add_digit(d):
            if len(password_var.get()) < 10:
                password_var.set(password_var.get() + d)

        def backspace():
            password_var.set(password_var.get()[:-1])

        def clear():
            password_var.set("")

        keys = [
            ("1", lambda: add_digit("1")), ("2", lambda: add_digit("2")), ("3", lambda: add_digit("3")),
            ("4", lambda: add_digit("4")), ("5", lambda: add_digit("5")), ("6", lambda: add_digit("6")),
            ("7", lambda: add_digit("7")), ("8", lambda: add_digit("8")), ("9", lambda: add_digit("9")),
            ("Del", backspace), ("0", lambda: add_digit("0")), ("Clear", clear),
        ]

        r = c = 0
        for txt, cmd in keys:
            tk.Button(
                numpad_frame,
                text=txt,
                command=cmd,
                font=("Arial", 18),
                width=5,
                height=2,
                bg=colors["active_bg"],
                fg=text_color
            ).grid(row=r, column=c, padx=8, pady=8)
            c += 1
            if c > 2:
                c = 0
                r += 1

        # --- Confirm Logic ---
        def confirm():
            entered = password_var.get()
            expected = self.shutdown_password.get()

            # Must enter something
            if not entered:
                return

            # ✅ SCRAMBLED PASSWORD CHECK
            if expected in entered:
                popup.destroy()
                self.password_popup_active = False

                # ✅ GRANT 15–20s STAFF GRACE
                self.grant_staff_auth()

                on_success()
            else:
                clear()
                self.show_message_box(self.get_text("invalid_password"))

        popup.bind("<Return>", lambda e: confirm())

        # Confirm + Cancel Buttons
        button_frame = tk.Frame(content, bg=content_bg)
        button_frame.pack(pady=20)

        tk.Button(
            button_frame,
            text=self.get_text("confirm"),
            command=confirm,
            font=("Arial", 14, "bold"),
            bg=colors["highlight"],
            fg="white",
            width=10,
            height=2
        ).pack(side="left", padx=15)

        tk.Button(
            button_frame,
            text=self.get_text("cancel"),
            command=lambda: (popup.destroy(), setattr(self, "password_popup_active", False)),
            font=("Arial", 14, "bold"),
            bg=colors["active_bg"],
            fg=text_color,
            width=10,
            height=2
        ).pack(side="left", padx=15)

    def prompt_for_reset_password(self):
        current = self.pages.get(CounterPage)
        if not current:
            return

        def do_reset():
            current.clear_mismatch()
            current.reset_counters()

        if self.staff_is_authenticated():
            do_reset()
        else:
            self.prompt_password(
                title=self.get_text("reset_counters_title"),
                message=self.get_text("enter_password"),
                on_success=do_reset
            )

    def safe_close_popup(self, popup):
        try:
            popup.grab_release()
        except Exception:
            pass
        try:
            popup.focus_release()
        except Exception:
            pass
        popup.destroy()

    def prompt_for_calibrate_password(self):
        current = self.pages.get(CounterPage)
        if not current:
            return

        def do_calibrate():
            self._after_calibrate_password(current)

        if self.staff_is_authenticated():
            do_calibrate()
        else:
            self.prompt_password(
                title=self.get_text("calibrate_system_title"),
                message=self.get_text("enter_password"),
                on_success=do_calibrate
            )



    def _after_calibrate_password(self, counter_page):
        # Clear mismatch FIRST
        counter_page.clear_mismatch()

        # Show calibration choice popup
        self.show_calibrate_choice_popup(counter_page)

    def show_calibrate_choice_popup(self, counter_page):
        colors = self.get_theme_colors()

        popup = tk.Toplevel(self)
        popup_width = 520
        popup_height = 360

        popup.overrideredirect(True)
        popup.configure(bg="black")
        self.center_popup(popup, popup_width, popup_height)
        popup.grab_set()
        popup.focus_set()

        panel = tk.Frame(popup, bg=colors["top_bg"], bd=2, relief="solid")
        panel.pack(fill="both", expand=True)

        # ---- TITLE BAR ----
        title_bar = tk.Frame(panel, bg="#2E7D32", height=60)
        title_bar.pack(fill="x")

        tk.Label(
            title_bar,
            text=self.get_text("calibrate_mode"),
            font=("Arial", 20, "bold"),
            fg="white",
            bg="#2E7D32"
        ).pack(pady=10)

        body = tk.Frame(panel, bg=colors["top_bg"])
        body.pack(expand=True, pady=25)

        tk.Label(
            body,
            text=self.get_text("calibration_option"),
            font=("Arial", 16, "bold"),
            bg=colors["top_bg"],
            fg=colors["fg_color"]
        ).pack(pady=10)

        controller = counter_page.controller

        # ---- ACTIONS ----
        def calibrate_summary_from_object():
            with controller.number_lock:
                object_count = controller.dome_count

                # ? Update controller state (REAL source of truth)
                controller.ribbon_count = object_count

                # ? Push to UI
                controller.push_counts_to_ui()

                # ? Reset mismatch logic
                controller.mismatch_active = False
                controller.mismatch_direction = None
                controller.mismatch_baseline_time = None

                # ? Publish calibrate command
                try:
                    if controller.mqtt_client:
                        controller.mqtt_client.publish(
                            "summary/calibrate",
                            str(object_count),
                            qos=1
                        )
                except Exception as e:
                    print("Calibrate publish failed:", e)

                controller.log_to_activity_log(
                    f"Calibration: Summary set to Object ({object_count})"
                )

            self.safe_close_popup(popup)


        def calibrate_object_from_summary():
            controller.dome_count = controller.ribbon_count
            counter_page.dome_count.set(controller.dome_count)

            # RESET mismatch (CRITICAL)
            controller.mismatch_active = False
            controller.mismatch_direction = None
            controller.mismatch_baseline_time = None

            controller.push_counts_to_ui()
            controller.log_to_activity_log(
                self.get_text("cal_obj_to_sum")
            )

            self.safe_close_popup(popup)

        # ---- BUTTONS ----
        btn_style = {
            "font": ("Arial", 14, "bold"),
            "width": 28,
            "height": 2,
            "cursor": "hand2"
        }

        tk.Button(
            body,
            text= self.get_text("sum_to_obj"),
            command=calibrate_summary_from_object,
            bg=colors["highlight"],
            fg="white",
            **btn_style
        ).pack(pady=8)

        tk.Button(
            body,
            text= self.get_text("obj_to_sum"),
            command=calibrate_object_from_summary,
            bg=colors["active_bg"],
            fg=colors["fg_color"],
            **btn_style
        ).pack(pady=8)

        tk.Button(
            body,
            text=self.get_text("cancel"),
            command=lambda: self.safe_close_popup(popup),
            bg="#cccccc",
            fg="black",
            font=("Arial", 12),
            width=12,
            height=1
        ).pack(pady=15)

    def prompt_for_exit_password(self):
        def do_exit():
            cp = self.pages.get(CounterPage)
            if cp:
                cp.clear_mismatch()
            self.show_page(HomePage)

        if self.staff_is_authenticated():
            do_exit()
        else:
            self.prompt_password(
                title=self.get_text("exit_counter_title"),
                message=self.get_text("enter_password"),
                on_success=do_exit
            )

    def center_popup(self, popup, width, height):
        popup.update_idletasks()
        x = (popup.winfo_screenwidth() // 2) - (width // 2)
        y = (popup.winfo_screenheight() // 2) - (height // 2)
        popup.geometry(f"{width}x{height}+{x}+{y}")

    def show_about_popup(self):
        colors = self.get_theme_colors()

        popup = tk.Toplevel(self)
        popup_width = 600
        popup_height = 550

        popup.overrideredirect(True)
        popup.grab_set()
        popup.focus_set()

        self.center_popup(popup, popup_width, popup_height)

        # theme color for main panel background
        panel = tk.Frame(popup, bg=colors["top_bg"], relief="solid", bd=2)
        panel.pack(fill="both", expand=True)

        # TITLE BAR (Green title bar is kept, only content below changes)
        title_bar = tk.Frame(panel, bg="#2E7D32", height=60)
        title_bar.pack(fill="x")

        tk.Label(
            title_bar,
            text=self.get_text("about_system"),
            font=("Arial", 20, "bold"),
            fg="white",
            bg="#2E7D32"
        ).pack(pady=10)

        # theme colors for the content box
        content_box_bg = colors["top_bg"]
        content_box = tk.Frame(panel, bg=content_box_bg, bd=1, relief="solid")
        content_box.pack(padx=30, pady=(20, 10), fill="both")

        tk.Label(
            content_box,
            text=self.get_text("about_info"),
            font=("Arial", 12),
            justify="left",
            bg=content_box_bg,
            fg=colors["fg_color"],
            padx=15,
            pady=15,
            wraplength=520
        ).pack(fill="x")

        # Close Button
        tk.Button(
            panel,
            text=self.get_text("close"),
            command=popup.destroy,
            font=("Arial", 14, "bold"),
            bg=colors["highlight"],
            fg="white",
            bd=0,
            width=10,
            height=2,
            cursor="hand2"
        ).pack(pady=20)

    def prompt_for_new_ip(self, settings_popup=None):
        """
        Displays a modal Toplevel window for changing the MQTT Broker IP address,
        including password confirmation and Numpad routing logic.
        """
        colors = self.get_theme_colors()
        panel_bg = colors["top_bg"]
        text_color = colors["fg_color"]
        input_bg = colors["input_bg"]
        input_fg = colors["input_fg"]
        active_bg = colors["active_bg"]
        highlight = colors["highlight"]

        popup = tk.Toplevel(self)
        popup_width = 850
        popup_height = 600

        popup.overrideredirect(True)
        popup.configure(bg="black")
        self.center_popup(popup, popup_width, popup_height)
        popup.grab_set()
        popup.focus_set()

        ip_var = tk.StringVar(value=self.mqtt_ip.get())
        current_password_var = tk.StringVar()

        # --- Stability Handlers ---
        def regain_ip_focus():
            """Forces the IP change popup to regain the modal grab."""
            popup.lift()
            popup.grab_set()

        def cancel_and_regain():
            """Destroys the IP popup and forces settings popup to regain focus."""
            popup.destroy()
            if settings_popup:
                settings_popup.lift()
                settings_popup.grab_set()

        # --- Input Constraints ---
        def limit_password_pin(*args):
            # Enforces password length and numeric input
            v = current_password_var.get()
            if not v.isdigit():
                current_password_var.set("".join(ch for ch in v if ch.isdigit()))
            if len(v) > 6:
                current_password_var.set(v[:6])

        current_password_var.trace_add("write", limit_password_pin)

        # --- IP Address Building Logic ---
        def append_to_ip(digit):
            current_ip = ip_var.get()
            octets = current_ip.split('.')
            last_segment = octets[-1] if octets else ""

            if digit == '.':
                # Allow dot only if: not empty, doesn't end in '.', has < 4 octets, and last segment is not empty
                if current_ip and not current_ip.endswith('.') and current_ip.count('.') < 3 and last_segment:
                    ip_var.set(current_ip + digit)
                return

            if digit.isdigit():
                # If starting fresh or after a dot, allow append
                if not current_ip or current_ip.endswith('.'):
                    ip_var.set(current_ip + digit)
                    return

                # If the current segment has less than 3 digits, allow append
                if len(last_segment) < 3:
                    ip_var.set(current_ip + digit)
                    return

                return

        # --- Numpad Handlers ---
        def numpad_input_handler(digit):
            """Routes digit/dot input to the currently focused entry field."""
            focused_widget = popup.focus_get()

            if focused_widget == current_entry:
                # Password Entry has focus (only digits allowed)
                if digit.isdigit():
                    current_password_var.set(current_password_var.get() + digit)
            elif focused_widget == ip_entry:
                # IP Entry has focus
                append_to_ip(digit)

        def delete_last():
            """Deletes last character from the currently focused entry field."""
            focused_widget = popup.focus_get()
            if focused_widget == current_entry:
                current_password_var.set(current_password_var.get()[:-1])
            elif focused_widget == ip_entry:
                ip_var.set(ip_var.get()[:-1])

        def clear_all():
            """Clears all text from the currently focused entry field."""
            focused_widget = popup.focus_get()
            if focused_widget == current_entry:
                current_password_var.set("")
            elif focused_widget == ip_entry:
                ip_var.set("")

        # --- Validation and Confirm Logic ---
        def is_valid_ip(ip):
            if not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                return False
            try:
                for octet in ip.split('.'):
                    if not 0 <= int(octet) <= 255:
                        return False
                return True
            except ValueError:
                return False

        def confirm_save_ip():
            new_ip = ip_var.get()
            current_password = current_password_var.get()

            if not is_valid_ip(new_ip):
                self.show_message_box(
                    self.get_text("invalid_ip_format"),
                    on_ok_callback=regain_ip_focus
                )
                return

            if current_password != self.shutdown_password.get():
                self.show_message_box(
                    self.get_text("invalid_password"),
                    on_ok_callback=regain_ip_focus
                )
                current_password_var.set("")
                current_entry.focus_set()
                return

            # --- SUCCESS AND SAVE ---
            self.mqtt_ip.set(new_ip)
            self.save_settings_to_file()

            popup.destroy()
            if settings_popup:
                settings_popup.destroy()

            self.show_message_box(
                self.get_text("sensitive_settings_saved"),
            )

        # --- GUI SETUP ---
        panel = tk.Frame(popup, bg=panel_bg, bd=2, relief="solid")
        panel.pack(fill="both", expand=True)

        # Title Bar
        title_bar = tk.Frame(panel, bg="#2E7D32", height=60)
        title_bar.pack(fill="x")
        tk.Label(
            title_bar,
            text=self.get_text("change_ip_title"),  # Translatable Title
            font=("Arial", 20, "bold"),
            fg="white",
            bg="#2E7D32"
        ).pack(pady=10)

        # Main Content Frame
        content = tk.Frame(panel, bg=panel_bg, padx=20, pady=20)
        content.pack(fill="both", expand=True)
        content.grid_columnconfigure(0, weight=1)
        content.grid_columnconfigure(1, weight=1)

        # --- LEFT SIDE: IP Display and Password Input ---
        left_frame = tk.Frame(content, bg=panel_bg)
        left_frame.grid(row=0, column=0, sticky='nsew', padx=10, pady=10)

        # New IP Label (Translatable)
        tk.Label(left_frame, text=self.get_text("enter_new_ip"),
                 font=("Arial", 16, "bold"), bg=panel_bg, fg=text_color).pack(pady=(20, 5))

        # IP Input/Display (tk.Entry for focus)
        entry_style = {
            'font': ("Arial", 24, 'bold'),
            'bg': input_bg,
            'fg': input_fg,
            'width': 20,
            'relief': 'sunken',
            'bd': 2,
            'justify': 'center',
            'state': 'readonly',  # Restrict manual typing
            'readonlybackground': input_bg
        }
        ip_entry = tk.Entry(left_frame, textvariable=ip_var, **entry_style)
        ip_entry.pack(pady=20, padx=10, fill='x')

        # Current Password Label (Translatable)
        tk.Label(left_frame, text=self.get_text("current_password"),
                 font=("Arial", 16, "bold"), bg=panel_bg, fg=text_color).pack(pady=(20, 5))

        # Password Input
        entry_options = {
            'font': ("Arial", 18),
            'width': 10,
            'justify': 'center',
            'show': '*',
            'bg': input_bg,
            'fg': input_fg
        }
        current_entry = tk.Entry(left_frame, textvariable=current_password_var, **entry_options)
        current_entry.pack(pady=5)
        current_entry.focus_set()  # Focus is initially on the password

        # --- RIGHT SIDE: Custom Numpad ---
        numpad_frame = tk.Frame(content, bg=panel_bg, padx=10, pady=10)
        numpad_frame.grid(row=0, column=1, sticky='nsew')
        numpad_frame.grid_rowconfigure((0, 1, 2, 3), weight=1)
        numpad_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)

        button_options = {
            'font': ("Arial", 16, 'bold'),
            'bg': active_bg,
            'fg': input_fg,
            'height': 2,
            'width': 6
        }

        buttons = [
            ('7', 0, 0), ('8', 0, 1), ('9', 0, 2),
            ('Del', 0, 3, delete_last),
            ('4', 1, 0), ('5', 1, 1), ('6', 1, 2),
            ('.', 1, 3),
            ('1', 2, 0), ('2', 2, 1), ('3', 2, 2),
            ('Clear', 2, 3, clear_all)
        ]

        # Place standard buttons
        for (text, row, col) in [(b[0], b[1], b[2]) for b in buttons if len(b) == 3]:
            tk.Button(
                numpad_frame,
                text=text,
                command=lambda t=text: numpad_input_handler(t),
                **button_options
            ).grid(row=row, column=col, sticky='nsew', padx=5, pady=5)

        # Place action buttons (Delete, Clear)
        for (text, row, col, command) in [b for b in buttons if len(b) == 4]:
            btn_style = button_options.copy()
            btn_style['bg'] = '#B71C1C'
            btn_style['fg'] = 'white'

            tk.Button(
                numpad_frame,
                text=text[:3],
                command=command,
                **btn_style
            ).grid(row=row, column=col, sticky='nsew', padx=5, pady=5)

        # Row 4: Large '0' button spanning 4 columns
        tk.Button(
            numpad_frame,
            text='0',
            command=lambda: numpad_input_handler('0'),
            **button_options
        ).grid(row=3, column=0, columnspan=4, sticky='nsew', padx=5, pady=5)

        # --- Confirm/Cancel Buttons (Bottom, Translatable) ---
        button_frame = tk.Frame(panel, bg=panel_bg, pady=10)
        button_frame.pack(pady=10)

        tk.Button(
            button_frame,
            text=self.get_text("confirm"),
            command=confirm_save_ip,
            font=("Arial", 14, "bold"),
            bg=highlight,
            fg="white",
            width=10,
            height=2
        ).pack(side="left", padx=15)

        tk.Button(
            button_frame,
            text=self.get_text("cancel"),
            command=cancel_and_regain,
            font=("Arial", 14, "bold"),
            bg=active_bg,
            fg=text_color,
            width=10,
            height=2
        ).pack(side="left", padx=15)

    def prompt_for_new_password(self, settings_popup=None):
        """
        Displays a modal Toplevel window for changing the shutdown password,
        and handles the actual state update and file save on successful confirmation.
        """
        colors = self.get_theme_colors()
        panel_bg = colors["top_bg"]
        text_color = colors["fg_color"]
        input_bg = colors["input_bg"]
        input_fg = colors["input_fg"]
        active_bg = colors["active_bg"]
        highlight = colors["highlight"]

        popup = tk.Toplevel(self)
        popup_width = 750  # Wider to fit 3 fields + numpad
        popup_height = 550

        popup.overrideredirect(True)
        popup.configure(bg="black")
        self.center_popup(popup, popup_width, popup_height)
        popup.grab_set()
        popup.focus_set()

        def regain_password_focus():
            """Forces the password change popup to regain the modal grab."""
            popup.lift()
            popup.grab_set()

        def cancel_and_regain():
            """Destroys the password popup and forces settings popup to regain focus."""
            popup.destroy()
            if settings_popup:
                settings_popup.lift()
                settings_popup.grab_set()

        panel = tk.Frame(popup, bg=panel_bg, bd=2, relief="solid")
        panel.pack(fill="both", expand=True)

        title_bar = tk.Frame(panel, bg="#2E7D32", height=60)
        title_bar.pack(fill="x")
        tk.Label(
            title_bar,
            text=self.get_text("change_password_title"),
            font=("Arial", 20, "bold"),
            fg="white",
            bg="#2E7D32"
        ).pack(pady=10)

        content = tk.Frame(panel, bg=panel_bg, padx=20, pady=20)
        content.pack(fill="both", expand=True)
        content.grid_columnconfigure(0, weight=1)
        content.grid_columnconfigure(1, weight=1)

        input_frame = tk.Frame(content, bg=panel_bg, padx=10, pady=10)
        input_frame.grid(row=0, column=0, sticky='nsew')

        current_password_var = tk.StringVar()
        new_password_var = tk.StringVar()
        confirm_password_var = tk.StringVar()

        def limit_pin(*args):
            for var in [current_password_var, new_password_var, confirm_password_var]:
                v = var.get()
                if not v.isdigit():
                    var.set("".join(ch for ch in v if ch.isdigit()))
                if len(v) > 10:
                    var.set(v[:10])

        current_password_var.trace_add("write", limit_pin)
        new_password_var.trace_add("write", limit_pin)
        confirm_password_var.trace_add("write", limit_pin)

        entry_options = {
            'font': ("Arial", 18),
            'width': 10,
            'justify': 'center',
            'show': '*',
            'bg': input_bg,
            'fg': input_fg
        }

        tk.Label(input_frame, text=self.get_text("current_password"),
                 font=("Arial", 14, "bold"), bg=panel_bg, fg=text_color).pack(pady=(15, 5))
        current_entry = tk.Entry(input_frame, textvariable=current_password_var, **entry_options)
        current_entry.pack(pady=5)

        tk.Label(input_frame, text=self.get_text("new_password"),
                 font=("Arial", 14, "bold"), bg=panel_bg, fg=text_color).pack(pady=(15, 5))
        new_entry = tk.Entry(input_frame, textvariable=new_password_var, **entry_options)
        new_entry.pack(pady=5)

        tk.Label(input_frame, text=self.get_text("confirm_new_password"),
                 font=("Arial", 14, "bold"), bg=panel_bg, fg=text_color).pack(pady=(15, 5))
        confirm_entry = tk.Entry(input_frame, textvariable=confirm_password_var, **entry_options)
        confirm_entry.pack(pady=5)

        current_entry.focus_set()

        numpad_frame = tk.Frame(content, bg=panel_bg, padx=10, pady=10)
        numpad_frame.grid(row=0, column=1, sticky='nsew')

        def add_digit(d):
            try:
                focused = self.focus_get()
                if isinstance(focused, tk.Entry) and focused.master is input_frame:
                    if len(focused.get()) < 6:
                        focused.insert(tk.END, d)
            except Exception:
                pass

        def backspace():
            try:
                focused = self.focus_get()
                if isinstance(focused, tk.Entry) and focused.master is input_frame:
                    focused.delete(len(focused.get()) - 1, tk.END)
            except Exception:
                pass

        def clear_field():
            try:
                focused = self.focus_get()
                if isinstance(focused, tk.Entry) and focused.master is input_frame:
                    focused.delete(0, tk.END)
            except Exception:
                pass

        keys = [
            ("1", lambda: add_digit("1")), ("2", lambda: add_digit("2")), ("3", lambda: add_digit("3")),
            ("4", lambda: add_digit("4")), ("5", lambda: add_digit("5")), ("6", lambda: add_digit("6")),
            ("7", lambda: add_digit("7")), ("8", lambda: add_digit("8")), ("9", lambda: add_digit("9")),
            ("Del", backspace), ("0", lambda: add_digit("0")), ("Clear", clear_field),
        ]

        r = c = 0
        for txt, cmd in keys:
            tk.Button(
                numpad_frame,
                text=txt,
                command=cmd,
                font=("Arial", 18),
                width=5,
                height=2,
                bg=active_bg,
                fg=text_color
            ).grid(row=r, column=c, padx=8, pady=8)
            c += 1
            if c > 2:
                c = 0
                r += 1

        def confirm():
            current = current_password_var.get()
            new = new_password_var.get()
            confirm_new = confirm_password_var.get()

            if not all(p for p in [current, new, confirm_new]):
                self.show_message_box(
                    self.get_text("password_cannot_be_empty"),
                    on_ok_callback=regain_password_focus
                )
                return

            if self.shutdown_password.get() not in current:
                self.show_message_box(
                    self.get_text("invalid_password"),
                    on_ok_callback=regain_password_focus
                )
                current_password_var.set("")
                current_entry.focus_set()
                return

            if new != confirm_new:
                self.show_message_box(
                    self.get_text("passwords_do_not_match"),
                    on_ok_callback=regain_password_focus
                )
                new_password_var.set("")
                confirm_password_var.set("")
                new_entry.focus_set()
                return

            self.shutdown_password.set(new)
            self.temp_shutdown_password.set(new)
            self.save_settings_to_file()

            popup.destroy()
            if settings_popup:
                settings_popup.destroy()

            self.show_message_box(
                self.get_text("sensitive_settings_saved")
            )

        button_frame = tk.Frame(panel, bg=panel_bg)
        button_frame.pack(pady=10)

        tk.Button(
            button_frame,
            text=self.get_text("confirm"),
            command=confirm,
            font=("Arial", 14, "bold"),
            bg=highlight,
            fg="white",
            width=10,
            height=2
        ).pack(side="left", padx=15)

        tk.Button(
            button_frame,
            text=self.get_text("cancel"),
            command=cancel_and_regain,
            font=("Arial", 14, "bold"),
            bg=active_bg,
            fg=text_color,
            width=10,
            height=2
        ).pack(side="left", padx=15)

    def show_settings_popup(self):
    
        colors = self.get_theme_colors()

        style = ttk.Style()
        style.theme_use("default")

        if self.current_theme.get() == "dark":
            style.configure(
                "Dark.TCombobox",
                padding=6,
                fieldbackground=colors["input_bg"],   # entry area
                background=colors["input_bg"],
                foreground=colors["input_fg"],
                selectbackground=colors["highlight"],
                selectforeground="white"
            )
        else:
            style.configure(
                "Light.TCombobox",
                padding=6,
                fieldbackground="white",
                background="white",
                foreground="black",
                selectbackground=colors["highlight"],
                selectforeground="black"
            )

        language_options = {
            "English": "en",
            "中文": "zh",
            "Bahasa Melayu": "ms",
            "தமிழ்": "ta"
        }

        # Reverse lookup for displaying current value
        reverse_language_options = {v: k for k, v in language_options.items()}

        # Save current settings locally for 'Cancel' logic
        self.temp_theme = tk.StringVar(value=self.current_theme.get())
        self.temp_language = tk.StringVar(value=self.current_language.get())
        self.temp_volume = tk.IntVar(value=self.volume.get())
        self.temp_mqtt_ip = tk.StringVar(value=self.mqtt_ip.get())
        self.temp_shutdown_password = tk.StringVar(value=self.shutdown_password.get())

        popup = tk.Toplevel(self)
        popup_width = 600
        popup_height = 800

        popup.overrideredirect(True)
        popup.configure(bg="black")
        self.center_popup(popup, popup_width, popup_height)
        popup.grab_set()

        popup.option_add("*TCombobox*Listbox.font", ("Arial", 14))
        popup.option_add("*TCombobox*Listbox.selectBackground", colors["highlight"])
        popup.option_add("*TCombobox*Listbox.selectForeground", "white")
        
        popup.bind("<Escape>", lambda e: popup.destroy())

        popup_bg = colors["top_bg"]
        content_bg = colors["top_bg"]

        panel = tk.Frame(popup, bg=popup_bg, bd=2, relief="solid")
        panel.pack(fill="both", expand=True)

        title_bar = tk.Frame(panel, bg="#2E7D32", height=60)
        title_bar.pack(fill="x")

        tk.Label(
            title_bar,
            text=self.get_text("settings"),
            font=("Arial", 20, "bold"),
            fg="white",
            bg="#2E7D32"
        ).pack(pady=10)

        content_frame = tk.Frame(panel, bg=content_bg, padx=20, pady=20)
        content_frame.pack(fill="x", expand=False)

        tk.Label(
            content_frame,
            text=self.get_text("settings"),
            font=("Arial", 18, "bold"),
            bg=content_bg,
            fg=colors["highlight"]
        ).pack(pady=(0, 20))
        ttk.Separator(content_frame, orient='horizontal').pack(fill='x', pady=5)

        label_options = {'font': ("Arial", 12, "bold"), 'bg': content_bg, 'fg': colors["fg_color"]}
        frame_options = {'bg': content_bg}
        radio_active_bg = colors["active_bg"] if self.current_theme.get() == "light" else content_bg
        radio_options = {
            'bg': content_bg,
            'fg': colors["fg_color"],
            'selectcolor': content_bg,
            'activebackground': radio_active_bg,
            'activeforeground': colors["fg_color"]
        }

        # Logo
        tk.Label(
            content_frame,
            text="Logo",
            **label_options
        ).pack(anchor='w', pady=(10, 4))

        logo_frame = tk.Frame(content_frame, bg=content_bg)
        logo_frame.pack(anchor='w', padx=20)

        # Reverse lookup: key → display name
        reverse_logo_options = {v["key"]: k for k, v in LOGO_OPTIONS.items()}

        self.temp_logo = tk.StringVar(
            value=reverse_logo_options.get(self.current_logo.get(), "Giant")
        )

        logo_dropdown = ttk.Combobox(
            logo_frame,
            textvariable=self.temp_logo,
            values=list(LOGO_OPTIONS.keys()),
            state="readonly",
            width=18,
            font=("Arial", 11),
            style="Dark.TCombobox" if self.current_theme.get() == "dark" else "Light.TCombobox"
        )
        logo_dropdown.pack(pady=8, anchor="w")

        # Volume
        tk.Label(content_frame, text=self.get_text("volume"), **label_options).pack(anchor='w', pady=(15, 5))
        volume_scale = tk.Scale(
            content_frame,
            from_=0, to=100,
            orient=tk.HORIZONTAL,
            variable=self.temp_volume,
            length=350,
            showvalue=True,
            bg=content_bg,
            fg=colors["fg_color"],
            troughcolor="#555555" if self.current_theme.get() == "dark" else "#dddddd",
            highlightbackground=content_bg,
            command=lambda v: self.adjust_speaker_volume(v)  # 👈 ADD THIS
        )
        volume_scale.pack(pady=5)

        # Theme
        tk.Label(content_frame, text=self.get_text("theme"), **label_options).pack(anchor='w', pady=(15, 5))
        theme_frame = tk.Frame(content_frame, **frame_options)
        theme_frame.pack(anchor='w', padx=20)

        tk.Radiobutton(
            theme_frame,
            text=self.get_text("light_mode"),
            variable=self.temp_theme,
            value="light",
            **radio_options
        ).pack(side=tk.LEFT, padx=10)

        tk.Radiobutton(
            theme_frame,
            text=self.get_text("dark_mode"),
            variable=self.temp_theme,
            value="dark",
            **radio_options
        ).pack(side=tk.LEFT, padx=10)

        # --- Language Dropdown ---
        tk.Label(
            content_frame,
            text=self.get_text("language"),
            **label_options
        ).pack(anchor='w', pady=(15, 5))

        lang_frame = tk.Frame(content_frame, bg=content_bg)
        lang_frame.pack(anchor='w', padx=20)

        language_display_var = tk.StringVar(
            value=reverse_language_options.get(self.temp_language.get(), "English")
        )

        language_dropdown = ttk.Combobox(
            lang_frame,
            textvariable=language_display_var,
            values=list(language_options.keys()),
            state="readonly",
            width=24,                      # ⬅ wider
            font=("Arial", 14),             # ⬅ taller
            style="Dark.TCombobox" if self.current_theme.get() == "dark" else "Light.TCombobox"
        )
        language_dropdown.pack(pady=8, anchor="w")

        def on_language_change(event=None):
            selected = language_display_var.get()
            self.temp_language.set(language_options[selected])

        language_dropdown.bind("<<ComboboxSelected>>", on_language_change)

        # MQTT IP (button)
        tk.Label(content_frame, text=self.get_text("mqtt_broker"), **label_options).pack(anchor='w', pady=(15, 5))
        ip_input_frame = tk.Frame(content_frame, **frame_options)
        ip_input_frame.pack(pady=5)

        tk.Label(
            ip_input_frame,
            textvariable=self.temp_mqtt_ip,
            font=("Arial", 12),
            bg=content_bg,
            fg=colors["fg_color"]
        ).pack(side=tk.LEFT, padx=5)

        tk.Button(
            ip_input_frame,
            text=self.get_text("change"),
            command=lambda: self.prompt_for_new_ip(popup),
            font=("Arial", 14),
            bg=colors["active_bg"],
            fg=colors["fg_color"],
            bd=1,
            width=10,
            height=1,
            cursor="hand2"
        ).pack(side=tk.LEFT, padx=10)

        self.ip_error_label = tk.Label(content_frame, text="", fg="red", bg=content_bg)
        self.ip_error_label.pack()

        # Shutdown Password (button)
        tk.Label(content_frame, text=self.get_text("shutdown_password_label"), **label_options).pack(anchor='w', pady=(15, 5))
        password_input_frame = tk.Frame(content_frame, **frame_options)
        password_input_frame.pack(pady=5)

        tk.Label(
            password_input_frame,
            text="******",
            font=("Arial", 12),
            bg=content_bg,
            fg=colors["fg_color"]
        ).pack(side=tk.LEFT, padx=5)

        tk.Button(
            password_input_frame,
            text=self.get_text("change"),
            command=lambda: self.prompt_for_new_password(popup),
            font=("Arial", 14),
            bg=colors["active_bg"],
            fg=colors["fg_color"],
            bd=1,
            width=10,
            height=1,
            cursor="hand2"
        ).pack(side=tk.LEFT, padx=10)

        button_frame = tk.Frame(panel, bg=popup_bg)
        button_frame.pack(side=tk.BOTTOM, pady=(10, 20))

        tk.Button(
            button_frame,
            text=self.get_text("save"),
            command=lambda: self.save_settings(popup),
            font=("Arial", 16, "bold"),
            bg=colors["highlight"],
            fg="white",
            bd=1,
            width=10,
            height=2,
            cursor="hand2"
        ).pack(side=tk.LEFT, padx=16, pady=6)

        tk.Button(
            button_frame,
            text=self.get_text("cancel"),
            command=popup.destroy,
            font=("Arial", 16, "bold"),
            bg=colors["active_bg"],
            fg=colors["fg_color"],
            bd=1,
            width=10,
            height=2,
            cursor="hand2"
        ).pack(side=tk.LEFT, padx=16, pady=6)

    def validate_ip(self, ip_address):
        """Simple regex validation for IPv4 format."""
        ip_regex = r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$"
        if re.match(ip_regex, ip_address):
            octets = ip_address.split('.')
            if all(0 <= int(octet) <= 255 for octet in octets):
                return True
        return False

    def save_settings(self, popup):
        """Validates IP and updates controller state for non-password settings."""
        new_ip = self.temp_mqtt_ip.get()

        self.ip_error_label.config(text="")

        if not self.validate_ip(new_ip):
            self.ip_error_label.config(text=self.get_text("invalid_ip"))
            return

        self.current_theme.set(self.temp_theme.get())
        self.current_language.set(self.temp_language.get())
        self.volume.set(self.temp_volume.get())
        self.mqtt_ip.set(new_ip)
        
        # Update logo setting: Convert display name back to key
        selected_logo_display = self.temp_logo.get()
        logo_key = LOGO_OPTIONS.get(selected_logo_display, {}).get("key", "giant")
        self.current_logo.set(logo_key)

        self.save_settings_to_file()
        self.apply_theme()

        # 🔄 FORCE PAGE REBUILD FOR LANGUAGE & LOGO CHANGE
        for page in self.pages.values():
            page.destroy()
        self.pages.clear()

        # Reload current page
        self.show_page(HomePage)

        popup.destroy()


    def staff_is_authenticated(self):
        return time.time() < self.staff_auth_until

    def grant_staff_auth(self):
        self.staff_auth_until = time.time() + self.STAFF_AUTH_GRACE
        print(f"[AUTH] Staff grace active ({self.STAFF_AUTH_GRACE}s)")

    def revoke_staff_auth(self):
        self.staff_auth_until = 0
        print("[AUTH] Staff grace revoked")


    # ======================================================
    # MQTT + COUNTING LOGIC (FROM INTEGRATION13)
    # ======================================================
    def start_mqtt(self):
        if self.mqtt_client:
            return  # prevent double-start

        self.mqtt_client = mqtt.Client()

        def on_connect(client, userdata, flags, rc):
            if rc == 0:
                client.subscribe("counter/data")
                client.subscribe("summary/data")
                client.subscribe("barcode/data")
                client.subscribe("mismatch/data")
                client.subscribe("payment/reset")
                print("MQTT Connected")
            else:
                print("MQTT Connection failed:", rc)

        def on_message(client, userdata, msg):

            if msg.topic == "payment/reset":
                payload = msg.payload.decode().strip()

                if payload != "PAYMENT_RESET":
                    return

                print("[PAYMENT] Valid PAYMENT_RESET received")

                now = time.time()

                with self.number_lock:
                    # 🔒 Freeze dome counting for 5 seconds
                    self.dome_freeze_until = now + 5.0

                    # Reset BOTH main counters
                    self.dome_count = 0
                    self.ribbon_count = 0

                    # ✅ Reset alert counters too
                    self.camera_block_count = 0
                    self.mismatch_count = 0

                    # ✅ Re-arm mismatch acknowledgement so it can trigger again properly
                    self.mismatch_acknowledged = False

                    # Tell ribbon/summary side to reset
                    client.publish("summary/reset", "RESET")

                    # Clear mismatch state
                    self.mismatch_active = False
                    self.mismatch_direction = None
                    self.mismatch_baseline_time = None
                    self.last_mismatch_state = None
                    self.mismatch_start_time = None
                    self.last_mismatch_time = None

                    # Clear rolling history
                    self.recent_dome_counts.clear()

                # Update UI immediately (will push dome/ribbon + camera_block + mismatch)
                self.push_counts_to_ui()

                self.log_to_activity_log("Payment reset → all counters reset, dome frozen for 5 seconds")
                return
            
            if not self.counter_mode_active:
                return

            pass

            payload = msg.payload.decode(errors="ignore").strip()
            now = time.time()

            with self.number_lock:

                # ---------- BARCODE / CAMERA EVENTS ----------
                if msg.topic == "barcode/data":
                    txt = payload.lower()

                    if txt == "blocked camera detected":
                        self.camera_block_count += 1
                        self.flash_event("camera_blocked")

                    elif txt == "blocked barcode detected":
                        self.flash_event("barcode_blocked")

                    elif txt == "barcode detected":
                        self.flash_event("barcode_detected")

                    elif txt == "no barcode detected":
                        pass

                # ---------- DOME COUNTER ----------
                elif msg.topic == "counter/data":
                    try:
                        data = json.loads(payload)
                        count = int(data.get("Objects Detected", 0))
                        now = time.time()

                        # 🚫 Ignore dome counts during freeze window
                        if now < self.dome_freeze_until:
                            # Keep dome at 0 visually
                            self.dome_count = 0
                            return

                        if count > 0 and now - self.last_dome_increment_time > self.dome_debounce_interval:
                            self.dome_count += count
                            self.last_dome_increment_time = now

                    except Exception as e:
                        print("[GUI] counter/data parse error:", e)

                # ---------- RIBBON COUNTER ----------
                elif msg.topic == "summary/data":
                    try:
                        if now - self.last_ribbon_adjust_time > self.ribbon_adjust_debounce:
                            self.ribbon_count = int(payload)
                            self.last_ribbon_adjust_time = now
                    except ValueError:
                        pass


                # ---------- MISMATCH STATE SETUP ONLY ----------

                if self.dome_count > self.ribbon_count:
                    if self.mismatch_direction != "dome_gt":
                        self.mismatch_direction = "dome_gt"
                        self.mismatch_baseline_time = now
                        print("[MISMATCH] Dome > Ribbon timer started")

                elif self.ribbon_count > self.dome_count:
                    if self.mismatch_direction != "ribbon_gt":
                        self.mismatch_direction = "ribbon_gt"
                        self.mismatch_baseline_time = now
                        print("[AUTO-FIX] Ribbon > Dome timer started")

                else:
                    # equal → reset
                    self.mismatch_direction = None
                    self.mismatch_baseline_time = None

            if self.mismatch_acknowledged and self.dome_count == self.ribbon_count:
                print("[MISMATCH] Counts reconciled — system re-armed")
                self.mismatch_acknowledged = False
                    
            self.push_counts_to_ui()

        self.mqtt_client.on_connect = on_connect
        self.mqtt_client.on_message = on_message

        try:
            self.mqtt_client.connect(self.mqtt_ip.get(), 1883)
            threading.Thread(
                target=self.mqtt_client.loop_forever,
                daemon=True
            ).start()
        except Exception as e:
            print("MQTT connection error:", e)


    def check_mismatch_timer(self):

        # 🚫 If mismatch already acknowledged, do nothing
        if self.mismatch_acknowledged:
            self.after(200, self.check_mismatch_timer)
            return

        if not self.counter_mode_active:
            self.after(200, self.check_mismatch_timer)
            return

        now = time.time()

        # ---------- OBJECT > SUMMARY ----------
        if self.mismatch_direction == "dome_gt" and self.mismatch_baseline_time:
            if now - self.mismatch_baseline_time >= 6:
                if not self.mismatch_active:
                    print("[MISMATCH] Theft confirmed after 6s")

                    self.mismatch_active = True
                    self.revoke_staff_auth()
                    self.mismatch_count += 1
                    self.push_counts_to_ui()

                    # 📸 MQTT → trigger DomeCam screenshot
                    try:
                        if self.mqtt_client:
                            self.mqtt_client.publish(
                                "mismatch/data",
                                "mismatch detected",
                                qos=1
                            )
                            print("[MQTT] Published mismatch/data -> mismatch detected")
                    except Exception as e:
                        print("[MQTT] Failed to publish mismatch:", e)

                    # Stop staff audio
                    self.staff_assist_active = False

                    # Alarm + relay
                    self.play_mismatch_audio_loop()
                    GPIO.output(RELAY_PIN, GPIO.HIGH)

                    # Force staff popup
                    self.after(0, self.counter_page_ref.staff_assistance)

                # lock & reset timer
                self.mismatch_direction = None
                self.mismatch_baseline_time = None

        # ---------- SUMMARY > OBJECT (AUTO FIX) ----------
        elif self.mismatch_direction == "ribbon_gt" and self.mismatch_baseline_time:
            if now - self.mismatch_baseline_time >= 6:
                print("[AUTO-FIX] Dome auto-incremented")

                self.dome_count = self.ribbon_count

                self.mismatch_direction = None
                self.mismatch_baseline_time = None

                self.push_counts_to_ui()

        # keep checking
        self.after(200, self.check_mismatch_timer)


    # ======================================================
    # UI UPDATE HELPERS
    # ======================================================
    def push_counts_to_ui(self):
        if not self.counter_page_ref:
            return

        # --- Object Count logging ---
        if self.dome_count != self.last_logged_dome:
            self.log_to_activity_log(
                f"Object Count updated: {self.dome_count}"
            )
            self.last_logged_dome = self.dome_count

        # --- Summary Count logging ---
        if self.ribbon_count != self.last_logged_ribbon:
            self.log_to_activity_log(
                f"Summary Count updated: {self.ribbon_count}"
            )
            self.last_logged_ribbon = self.ribbon_count

        # UI updates
        self.counter_page_ref.dome_count.set(self.dome_count)
        self.counter_page_ref.ribbon_count.set(self.ribbon_count)
        self.counter_page_ref.camera_block_alerts.set(self.camera_block_count)
        self.counter_page_ref.mismatch_alerts.set(self.mismatch_count)

    # ======================================================
    # AUDIO (NON-OVERLAPPING, DEBOUNCED)
    # ======================================================
    def play_audio(self, key_or_filename):
        now = time.time()

        # Debounce same audio
        if key_or_filename in audio_map:
            filename = self.get_audio_file(key_or_filename)
            if not filename:
                return
        else:
            # Fallback: old hardcoded wav
            filename = key_or_filename

        # Prevent overlap
        if self.audio_playing:
            return

        base_path = "basic_pipelines/audios"
        file_path = os.path.join(base_path, filename)

        if not os.path.exists(file_path):
            print("Audio file missing:", file_path)
            return

        def _play():
            with self.audio_lock:
                try:
                    self.audio_playing = True
                    self.last_audio_played = filename
                    self.last_audio_time = time.time()
                    playsound(file_path)
                except Exception as e:
                    print("Audio error:", e)
                finally:
                    self.audio_playing = False

        threading.Thread(target=_play, daemon=True).start()

    # ======================================================
    # RELAY / VISUAL ALERT (SAFE, DEBOUNCED)
    # ======================================================
    def trigger_relay(self):
        if self.relay_active:
            return

        def _relay_task():
            try:
                self.relay_active = True
                GPIO.output(RELAY_PIN, GPIO.HIGH)
                time.sleep(5)
            except Exception as e:
                print("Relay error:", e)
            finally:
                try:
                    GPIO.output(RELAY_PIN, GPIO.LOW)
                except Exception:
                    pass
                self.relay_active = False

        threading.Thread(target=_relay_task, daemon=True).start()
    
    def bring_gui_to_front(self):
        """
        Ensures this GUI stays above other windows (Hailo, terminals, RTSP).
        """
        self.attributes("-topmost", True)
        self.update()
        self.after(200, lambda: self.attributes("-topmost", False))


class LogPage(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=controller.cget('bg'))
        self.controller = controller
        self.pack_propagate(False)
        colors = controller.get_theme_colors()

        content_frame = tk.Frame(self, bg=controller.cget('bg'))
        content_frame.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(
            content_frame,
            text=controller.get_text("activity_log").replace("✍️ ", ""),
            font=("Arial", 28, "bold"),
            bg=controller.cget('bg'),
            fg=colors["fg_color"]
        ).pack(pady=20)

        tk.Button(
            content_frame,
            text="← Back to Home",
            font=("Arial", 16),
            command=lambda: controller.show_page(HomePage),
            bg=colors["top_bg"],
            fg=colors["fg_color"],
            activebackground=colors["active_bg"],
            bd=0,
            cursor="hand2"
        ).pack(pady=(0, 20))

        tk.Label(
            content_frame,
            text="Detailed log of system events, security alerts, and camera activity",
            font=("Arial", 14),
            fg="#555555" if controller.current_theme.get() == "light" else "#aaaaaa",
            bg=colors["input_bg"],
            width=60,
            height=20,
            relief="groove"
        ).pack(pady=40)


class CameraPage1(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=controller.cget('bg'))
        self.controller = controller
        self.pack_propagate(False)
        colors = controller.get_theme_colors()

        content_frame = tk.Frame(self, bg=controller.cget('bg'))
        content_frame.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(
            content_frame,
            text=controller.get_text("dome_camera"),
            font=("Arial", 28, "bold"),
            bg=controller.cget('bg'),
            fg=colors["fg_color"]
        ).pack(pady=20)

        tk.Button(
            content_frame,
            text="← Back to Home",
            font=("Arial", 16),
            command=lambda: controller.show_page(HomePage),
            bg=colors["top_bg"],
            fg=colors["fg_color"],
            activebackground=colors["active_bg"],
            bd=0,
            cursor="hand2"
        ).pack(pady=(0, 20))

        tk.Label(
            content_frame,
            text="(Dome Camera Preview Placeholder: Live Feed Integration Here)",
            font=("Arial", 14),
            fg="#555555" if controller.current_theme.get() == "light" else "#aaaaaa",
            bg=colors["input_bg"],
            width=60,
            height=20,
            relief="groove"
        ).pack(pady=40)


class CameraPage2(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=controller.cget('bg'))
        self.controller = controller
        self.pack_propagate(False)
        colors = controller.get_theme_colors()

        content_frame = tk.Frame(self, bg=controller.cget('bg'))
        content_frame.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(
            content_frame,
            text=controller.get_text("ribbon_camera"),
            font=("Arial", 28, "bold"),
            bg=controller.cget('bg'),
            fg=colors["fg_color"]
        ).pack(pady=20)

        tk.Button(
            content_frame,
            text="← Back to Home",
            font=("Arial", 16),
            command=lambda: controller.show_page(HomePage),
            bg=colors["top_bg"],
            fg=colors["fg_color"],
            activebackground=colors["active_bg"],
            bd=0,
            cursor="hand2"
        ).pack(pady=(0, 20))

        tk.Label(
            content_frame,
            text="(Barcode Camera Preview Placeholder: Live Feed Integration Here)",
            font=("Arial", 14),
            fg="#555555" if controller.current_theme.get() == "light" else "#aaaaaa",
            bg=colors["input_bg"],
            width=60,
            height=20,
            relief="groove"
        ).pack(pady=40)


# ======================================================
# COUNTER PAGE (CONNECTED TO NEW LOGIC)
# ======================================================
class CounterPage(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=controller.cget('bg'))
        self.controller = controller
        self.pack_propagate(False)
        colors = controller.get_theme_colors()

        controller.counter_page_ref = self
        controller.start_mqtt()
        controller.init_flash_screen()

        # UI-bound variables (RESUME FROM CONTROLLER STATE)
        self.dome_count = tk.IntVar(value=controller.dome_count)
        self.ribbon_count = tk.IntVar(value=controller.ribbon_count)
        self.camera_block_alerts = tk.IntVar(value=controller.camera_block_count)
        self.mismatch_alerts = tk.IntVar(value=controller.mismatch_count)

        center = tk.Frame(self, bg=controller.cget('bg'))
        center.place(relx=0.5, rely=0.5, anchor="center")

        counter_frame = tk.Frame(center, bg=controller.cget('bg'))
        counter_frame.pack(pady=(20, 50))

        dome_group = tk.Frame(counter_frame, bg=controller.cget('bg'))
        dome_group.pack(side=tk.LEFT, padx=50)

        tk.Label(
            dome_group,
            text=controller.get_text("object_count"),
            font=("Arial", 20, "bold"),
            bg=controller.cget('bg'),
            fg=colors["fg_color"]
        ).pack()
        tk.Label(
            dome_group,
            textvariable=self.dome_count,
            font=("Arial", 120, "bold"),
            bg=controller.cget('bg'),
            fg=colors["highlight"]
        ).pack()

        ribbon_group = tk.Frame(counter_frame, bg=controller.cget('bg'))
        ribbon_group.pack(side=tk.LEFT, padx=50)

        tk.Label(
            ribbon_group,
            text=controller.get_text("number_count"),
            font=("Arial", 20, "bold"),
            bg=controller.cget('bg'),
            fg=colors["fg_color"]
        ).pack()
        tk.Label(
            ribbon_group,
            textvariable=self.ribbon_count,
            font=("Arial", 120, "bold"),
            bg=controller.cget('bg'),
            fg=colors["highlight"]
        ).pack()

        button_frame = tk.Frame(center, bg=controller.cget('bg'))
        button_frame.pack(pady=40)

        button_style = {
            'font': ("Arial", 16, "bold"),
            'width': 10,
            'height': 2,
            'bd': 1,
            'relief': 'raised',
            'cursor': 'hand2'
        }

        tk.Button(
            button_frame,
            text=controller.get_text("exit"),
            command=controller.prompt_for_exit_password,
            bg="#f5c7c7" if controller.current_theme.get() == "light" else "#6b3f3f",
            fg="black" if controller.current_theme.get() == "light" else "white",
            **button_style
        ).pack(side=tk.LEFT, padx=30)

        tk.Button(
            button_frame,
            text=controller.get_text("reset"),
            command=controller.prompt_for_reset_password,
            bg=colors["active_bg"],
            fg=colors["fg_color"],
            **button_style
        ).pack(side=tk.LEFT, padx=30)

        tk.Button(
            button_frame,
            text=controller.get_text("calibrate"),
            command=controller.prompt_for_calibrate_password,
            bg="#3ed879" if controller.current_theme.get() == "light" else "#6b3f3f",
            fg="black" if controller.current_theme.get() == "light" else "white",
            **button_style
        ).pack(side=tk.LEFT, padx=30)

        alert_frame = tk.Frame(self, bg=controller.cget('bg'))
        alert_frame.pack(side=tk.BOTTOM, fill='x', padx=30, pady=20)
        alert_frame.grid_columnconfigure(0, weight=1)

        alerts_left = tk.Frame(alert_frame, bg=controller.cget('bg'))
        alerts_left.grid(row=0, column=0, sticky='w')

        tk.Label(
            alerts_left,
            text=controller.get_text("camera_block_alert").replace("⚠️ ", ""),
            font=("Arial", 12),
            bg=controller.cget('bg'),
            fg=colors["fg_color"]
        ).grid(row=0, column=0, padx=5, pady=2, sticky="w")
        tk.Label(
            alerts_left,
            textvariable=self.camera_block_alerts,
            font=("Arial", 12, "bold"),
            bg=controller.cget('bg'),
            fg="red"
        ).grid(row=0, column=1, sticky="w")

        tk.Label(
            alerts_left,
            text=controller.get_text("mismatch_alert").replace("⚠️ ", ""),
            font=("Arial", 12),
            bg=controller.cget('bg'),
            fg=colors["fg_color"]
        ).grid(row=1, column=0, padx=5, pady=2, sticky="w")
        tk.Label(
            alerts_left,
            textvariable=self.mismatch_alerts,
            font=("Arial", 12, "bold"),
            bg=controller.cget('bg'),
            fg="red"
        ).grid(row=1, column=1, sticky="w")

        tk.Button(
            alert_frame,
            text=controller.get_text("staff_assistance"),
            font=("Arial", 12, "bold"),
            bg=colors["highlight"],
            fg="black" if controller.current_theme.get() == "light" else "white",
            height=2,
            width=15,
            bd=1,
            cursor="hand2",
            command=self.staff_assistance
        ).grid(row=0, column=1, rowspan=2, sticky='e')

    def reset_counters(self):
        controller = self.controller

        # --- Reset main counts ---
        controller.dome_count = 0
        controller.ribbon_count = 0
        self.dome_count.set(0)
        self.ribbon_count.set(0)

        # --- Reset alert counters ---
        controller.mismatch_count = 0
        controller.camera_block_count = 0
        self.mismatch_alerts.set(0)
        self.camera_block_alerts.set(0)

        # --- Reset mismatch state ---
        controller.mismatch_active = False
        controller.mismatch_direction = None
        controller.mismatch_baseline_time = None
        controller.last_logged_mismatch_dome_count = 0

        # --- Stop alarms & relay ---
        controller.staff_assist_active = False
        GPIO.output(RELAY_PIN, GPIO.LOW)

        # --- Reset activity log debounce ---
        controller.last_logged_dome = 0
        controller.last_logged_ribbon = 0

        # --- Log reset ---
        controller.log_to_activity_log("System Reset: Success")

        # --- Publish MQTT reset ---
        try:
            if controller.mqtt_client:
                controller.mqtt_client.publish("summary/reset", "RESET")
        except Exception as e:
            print("Failed to publish reset:", e)

    def show_assistance_popup(self):
        controller = self.controller
        colors = controller.get_theme_colors()

        def acknowledge():
            popup.destroy()
            self.clear_mismatch()

        popup = tk.Toplevel(controller)
        popup.overrideredirect(True)
        popup.configure(bg="black")
        controller.center_popup(popup, 500, 300)
        popup.grab_set()

        panel = tk.Frame(popup, bg=colors["top_bg"], bd=2, relief="solid")
        panel.pack(fill="both", expand=True)

        title_bar = tk.Frame(panel, bg="#2E7D32", height=60)
        title_bar.pack(fill="x")

        tk.Label(
            title_bar,
            text=controller.get_text("staff_assistance"),
            font=("Arial", 20, "bold"),
            fg="white",
            bg="#2E7D32"
        ).pack(pady=10)

        tk.Label(
            panel,
            text=controller.get_text("require_staff_assistance"),
            font=("Arial", 16, "bold"),
            fg=colors["fg_color"],
            bg=colors["top_bg"]
        ).pack(pady=40)

        tk.Button(
            panel,
            text=controller.get_text("ok"),
            command=acknowledge,
            font=("Arial", 14, "bold"),
            bg=colors["highlight"],
            fg="white",
            width=10,   
            height=2
        ).pack(pady=10)

    def show_mismatch_popup(self):
        controller = self.controller
        colors = controller.get_theme_colors()

        def acknowledge():
            popup.destroy()

            # 🔐 Require admin password
            self.controller.prompt_password(
                title=self.controller.get_text("confirm"),
                message=self.controller.get_text("enter_password"),
                on_success=self.clear_mismatch
            )

        popup = tk.Toplevel(controller)
        popup.overrideredirect(True)
        popup.configure(bg="black")
        controller.center_popup(popup, 500, 300)
        popup.grab_set()

        panel = tk.Frame(popup, bg=colors["top_bg"], bd=2, relief="solid")
        panel.pack(fill="both", expand=True)

        title_bar = tk.Frame(panel, bg="#2E7D32", height=60)
        title_bar.pack(fill="x")

        tk.Label(
            title_bar,
            text=controller.get_text("staff_assistance"),
            font=("Arial", 20, "bold"),
            fg="white",
            bg="#2E7D32"
        ).pack(pady=10)

        tk.Label(
            panel,
            text=controller.get_text("require_staff_assistance"),
            font=("Arial", 16, "bold"),
            fg=colors["fg_color"],
            bg=colors["top_bg"]
        ).pack(pady=40)

        tk.Button(
            panel,
            text=controller.get_text("ok"),
            command=acknowledge,
            font=("Arial", 14, "bold"),
            bg=colors["highlight"],
            fg="white",
            width=10,   
            height=2
        ).pack(pady=10)

    def clear_mismatch(self):
        controller = self.controller

        controller.mismatch_active = False
        controller.staff_assist_active = False   # 🔇 stop staff loop too

        # 🔒 Stop ALL mismatch logic
        controller.mismatch_direction = None
        controller.mismatch_baseline_time = None

        controller.staff_assist_active = False

        GPIO.output(RELAY_PIN, GPIO.LOW)

        controller.mismatch_start_time = None
        controller.last_logged_mismatch_dome_count = controller.dome_count

        print("[MISMATCH] Incident cleared by staff")

        if controller.mqtt_client:
            controller.mqtt_client.publish("incident/clear", "CLEAR", qos=1)
    
    def staff_assistance(self):
        # 🚫 Do not interrupt existing password dialogs
        if self.controller.password_popup_active:
            print("[STAFF] Password popup active — mismatch popup suppressed")
            return
    
        # 🚫 Do not start staff audio if mismatch is active
        if self.controller.mismatch_active:
            print("[STAFF] Blocked: mismatch alarm active")
            self.show_mismatch_popup()
            return

        if self.controller.staff_assist_active:
            return

        self.controller.staff_assist_active = True
        self.controller.play_staff_assist_audio()

        GPIO.output(RELAY_PIN, GPIO.HIGH)
        print("[STAFF] Relay ON")

        self.show_assistance_popup()

# ======================================================
# HOME PAGE
# ======================================================
class HomePage(tk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent, bg=controller.cget('bg'))
        self.controller = controller
        self.logo_tk = None

        center = tk.Frame(self, bg=controller.cget('bg'))
        center.place(relx=0.5, rely=0.5, anchor="center")
        center.grid_columnconfigure(0, weight=1)
        center.grid_columnconfigure(1, weight=1)

        # logo_path = "/home/pi/hailo-rpi5-examples/images/Giant Logo.png"
        logo_key = controller.current_logo.get()
        logo_path = next(
            (v["path"] for v in LOGO_OPTIONS.values() if v["key"] == logo_key),
            None
        )

        if logo_key == "prime":
            logo_size = (500, 180)
        else:  # giant or default
            logo_size = (300, 180)

        try:
            img = Image.open(logo_path).resize(logo_size)
            self.logo_tk = ImageTk.PhotoImage(img)
            tk.Label(center, image=self.logo_tk, bg=controller.cget('bg')).grid(
                row=0, column=0, columnspan=2, pady=(0, 40)
            )
        except Exception:
            tk.Label(
                center,
                text="Supermarket",
                font=("Arial", 32),
                bg=controller.cget('bg'),
                fg=controller.get_theme_colors()["fg_color"]
            ).grid(row=0, column=0, columnspan=2, pady=(0, 40))

        colors = controller.get_theme_colors()
        button_style = {
            'font': ("Arial", 18),
            'width': 18,
            'height': 2,
            'bg': colors["top_bg"],
            'fg': colors["fg_color"],
            'activebackground': colors["active_bg"],
            'bd': 1,
            'cursor': 'hand2'
        }

        tk.Button(center, text=controller.get_text("dome_camera"),
                    command=lambda: controller.show_page(CameraPage1),
                    **button_style).grid(row=1, column=0, padx=15, pady=10)

        tk.Button(center, text=controller.get_text("ribbon_camera"),
                    command=lambda: controller.show_page(CameraPage2),
                    **button_style).grid(row=1, column=1, padx=15, pady=10)

        tk.Button(center,text=controller.get_text("activity_log").replace("✍️ ", ""),
                    command=controller.show_activity_log,
                    **button_style).grid( row=2,column=0,columnspan=2,padx=15,pady=10,sticky="n")

        start_style = button_style.copy()
        start_style.update({
            'width': 10,
            'font': ("Arial", 16),
            'bg': colors["highlight"],
            'fg': "black" if controller.current_theme.get() == "light" else "white"
        })

        tk.Button(center, text=controller.get_text("start"),
                  command=lambda: controller.show_page(CounterPage),
                  **start_style).grid(row=3, column=0, columnspan=2, pady=30)

if __name__ == "__main__":
    app = PageController()
    app.mainloop()