import colorsys
import concurrent.futures
import hashlib
import os
import random
import shutil
import uuid
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from fabric.utils.helpers import exec_shell_command_async
from fabric.widgets.box import Box
from fabric.widgets.button import Button
from fabric.widgets.entry import Entry
from fabric.widgets.label import Label
from fabric.widgets.scrolledwindow import ScrolledWindow
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango
from PIL import Image, ImageOps

import config.config
import config.data as data
import modules.icons as icons

class WallpaperSelector(Box):
    CACHE_DIR = f"{data.CACHE_DIR}/thumbs"

    def __init__(self, **kwargs):
        # Очищаем старый мусор, если структура папок изменилась
        old_cache_dir = f"{data.CACHE_DIR}/wallpapers"
        if os.path.exists(old_cache_dir):
            shutil.rmtree(old_cache_dir)

        super().__init__(
            name="wallpapers",
            spacing=4,
            orientation="v",
            h_expand=False,
            v_expand=False,
            **kwargs,
        )
        
        # Создаем папку для кэша
        os.makedirs(self.CACHE_DIR, exist_ok=True)

        self.files = []
        self.thumbnails = []
        self.thumbnail_queue = []
        self.executor = ThreadPoolExecutor(max_workers=2) # 2 потока достаточно, 4 могут вешать I/O
        self.selected_index = -1
        self.matugen_enabled = self._load_matugen_state()

        # UI Initialization
        self._init_ui()
        
        # Загрузка обоев
        GLib.idle_add(self._load_wallpapers_async().__next__)

    def _init_ui(self):
        """Выносим инициализацию UI в отдельный метод для чистоты __init__"""
        self.viewport = Gtk.IconView(name="wallpaper-icons")
        self.viewport.set_model(Gtk.ListStore(GdkPixbuf.Pixbuf, str))
        self.viewport.set_pixbuf_column(0)
        self.viewport.set_text_column(-1)
        self.viewport.set_item_width(0)
        self.viewport.connect("item-activated", self.on_wallpaper_selected)

        self.scrolled_window = ScrolledWindow(
            name="scrolled-window",
            spacing=10,
            h_expand=True,
            v_expand=True,
            child=self.viewport,
            propagate_width=False,
            propagate_height=False,
        )

        self.search_entry = Entry(
            name="search-entry-walls",
            placeholder="Search Wallpapers...",
            h_expand=True,
            h_align="fill",
            notify_text=lambda entry, *_: self.arrange_viewport(entry.get_text()),
            on_key_press_event=self.on_search_entry_key_press,
        )
        self.search_entry.connect("focus-out-event", lambda w, e: False) # Fix focus loss

        self.schemes = {
            "scheme-tonal-spot": "Tonal Spot",
            "scheme-content": "Content",
            "scheme-expressive": "Expressive",
            "scheme-fidelity": "Fidelity",
            "scheme-fruit-salad": "Fruit Salad",
            "scheme-monochrome": "Monochrome",
            "scheme-neutral": "Neutral",
            "scheme-rainbow": "Rainbow",
        }

        self.scheme_dropdown = Gtk.ComboBoxText()
        self.scheme_dropdown.set_name("scheme-dropdown")
        for key, display_name in self.schemes.items():
            self.scheme_dropdown.append(key, display_name)
        self.scheme_dropdown.set_active_id("scheme-tonal-spot")

        self.matugen_switcher = Gtk.Switch(name="matugen-switcher")
        self.matugen_switcher.set_active(self.matugen_enabled)
        self.matugen_switcher.connect("notify::active", self.on_switch_toggled)

        self.random_wall = Button(
            name="random-wall-button",
            child=Label(name="random-wall-label", markup=icons.dice_1),
            tooltip_text="Random Wallpaper",
        )
        self.random_wall.connect("clicked", self.set_random_wallpaper)

        header_box = Box(
            name="header-box",
            spacing=8,
            orientation="h",
            children=[self.random_wall, self.search_entry, self.scheme_dropdown, self.matugen_switcher],
        )
        self.add(header_box)

        # Color Selector
        self.hue_slider = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=Gtk.Adjustment(value=0, lower=0, upper=360, step_increment=1, page_increment=10),
            draw_value=False,
            name="hue-slider",
        )
        self.hue_slider.set_hexpand(True)
        
        self.apply_color_button = Button(
            name="apply-color-button",
            child=Label(markup=icons.accept),
        )
        self.apply_color_button.connect("clicked", self.on_apply_color_clicked)

        self.custom_color_selector_box = Box(
            orientation="h", spacing=5, name="custom-color-selector-box",
            children=[self.hue_slider, self.apply_color_button]
        )
        
        self.pack_start(self.scrolled_window, True, True, 0)
        self.pack_start(self.custom_color_selector_box, False, False, 0)
        
        self.connect("map", self.on_map)
        self.randomize_dice_icon()

    def _load_matugen_state(self):
        try:
            if os.path.exists(data.MATUGEN_STATE_FILE):
                with open(data.MATUGEN_STATE_FILE, "r") as f:
                    return f.read().strip().lower() == "true"
        except Exception:
            pass
        return True

    def _load_wallpapers_async(self):
        """Сканируем папку и загружаем файлы без лишних переименований"""
        if not os.path.exists(data.WALLPAPERS_DIR):
            os.makedirs(data.WALLPAPERS_DIR, exist_ok=True)
            
        # Просто читаем список файлов. Никаких os.rename здесь!
        all_files = os.listdir(data.WALLPAPERS_DIR)
        
        # Фильтруем и сортируем
        self.files = sorted([
            f for f in all_files 
            if self._is_image_or_video(f) and not f.startswith(".")
        ])

        # Запускаем генерацию превью
        self._start_thumbnail_thread()
        yield False

    def _start_thumbnail_thread(self):
        # Запускаем в отдельном потоке, чтобы не морозить GUI
        thread = GLib.Thread.new("thumbnail-loader", self._preload_thumbnails, None)

    def _preload_thumbnails(self, _data):
        futures = [
            self.executor.submit(self._process_file, file_name)
            for file_name in self.files
        ]
        concurrent.futures.wait(futures)
        GLib.idle_add(self._process_batch)

    def _process_file(self, file_name):
        full_path = os.path.join(data.WALLPAPERS_DIR, file_name)
        cache_path = self._get_cache_path(file_name)
        
        # Если кэш уже есть — используем
        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
            self.thumbnail_queue.append((cache_path, file_name))
            GLib.idle_add(self._process_batch)
            return

        is_video = file_name.lower().endswith(('.mp4', '.mkv', '.webm', '.mov'))
        temp_cache_path = cache_path + f".tmp.{uuid.uuid4().hex}"

        try:
            if is_video:
                # Шаг 1: Выдергиваем полный кадр во временный файл (без ресайза)
                # Это быстро и надежно
                full_frame_path = temp_cache_path + ".full.jpg"
                cmd = [
                    "ffmpeg", "-y", "-v", "error",
                    "-i", full_path, 
                    "-ss", "00:00:01", 
                    "-vframes", "1", 
                    "-f", "image2",
                    full_frame_path
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                # Шаг 2: Если кадр есть — ресайзим и кропаем через PIL (как обычную картинку)
                if os.path.exists(full_frame_path) and os.path.getsize(full_frame_path) > 0:
                    try:
                        with Image.open(full_frame_path) as img:
                            thumb = ImageOps.fit(img, (96, 96), method=Image.Resampling.LANCZOS)
                            thumb.save(temp_cache_path, "PNG")
                    finally:
                        # Удаляем полный кадр
                        os.remove(full_frame_path)
            else:
                # Картинки (PIL)
                with Image.open(full_path) as img:
                    if img.mode in ('RGBA', 'P'): img = img.convert('RGB')
                    thumb = ImageOps.fit(img, (96, 96), method=Image.Resampling.LANCZOS)
                    thumb.save(temp_cache_path, "PNG")

            # ФИНАЛЬНАЯ ПРОВЕРКА: Если все попытки провалились — генерируем заглушку
            if not os.path.exists(temp_cache_path) or os.path.getsize(temp_cache_path) == 0:
                print(f"Failed to generate thumb for {file_name}, creating fallback.")
                fallback = Image.new('RGB', (96, 96), color=(45, 45, 45)) # Темно-серый фон
                draw = ImageDraw.Draw(fallback)
                # Рисуем "VIDEO" или "IMG"
                text = "VIDEO" if is_video else "IMG"
                # (Позиционирование примерное, по центру)
                draw.text((30, 40), text, fill=(200, 200, 200)) 
                fallback.save(temp_cache_path, "PNG")

            # Теперь файл точно существует (или превью, или заглушка)
            os.replace(temp_cache_path, cache_path)
            self.thumbnail_queue.append((cache_path, file_name))
            GLib.idle_add(self._process_batch)

        except Exception as e:
            print(f"Critical error processing {file_name}: {e}")
            # Даже при ошибке чистим мусор
            if os.path.exists(temp_cache_path):
                os.remove(temp_cache_path)


    def _process_batch(self):
        if not self.thumbnail_queue:
            return False
            
        batch = self.thumbnail_queue[:10]
        del self.thumbnail_queue[:10]
        
        model = self.viewport.get_model()
        for cache_path, file_name in batch:
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file(cache_path)
                self.thumbnails.append((pixbuf, file_name))
                model.append([pixbuf, file_name])
            except Exception as e:
                print(f"Error loading pixbuf {cache_path}: {e}")
        
        return len(self.thumbnail_queue) > 0

    def _apply_wallpaper(self, file_name):
        """Единый метод для установки обоев (awww + mpvpaper)"""
        full_path = os.path.join(data.WALLPAPERS_DIR, file_name)
        is_video = file_name.lower().endswith(('.mp4', '.mkv', '.webm', '.mov'))
        
        selected_scheme = self.scheme_dropdown.get_active_id()
        current_wall_link = os.path.expanduser("~/.current.wall")

        # 1. Очистка (убиваем mpvpaper, awww обычно не надо убивать, но на всякий случай)
        # awww работает как сервис, можно просто слать новые команды
        subprocess.run("killall -q mpvpaper", shell=True)
        time.sleep(0.1)

        # 2. Установка обоев
        if is_video:
            print(f"Setting video: {file_name}")
            # mpvpaper (как и было)
            mpv_cmd = [
                "mpvpaper",
                "-o", "loop-file=inf --no-audio --hwdec=auto-safe --vo=gpu --gpu-context=wayland",
                "*",
                full_path
            ]
            subprocess.Popen(
                mpv_cmd, 
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL, 
                start_new_session=True
            )
            
            # Matugen & Symlink (как и было, без изменений)
            tmp_thumb = os.path.join("/tmp", f"ax_wall_{uuid.uuid4().hex}.jpg")
            subprocess.run(
                ["ffmpeg", "-y", "-i", full_path, "-vframes", "1", "-f", "image2", tmp_thumb],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            
            if os.path.exists(current_wall_link) or os.path.islink(current_wall_link):
                os.remove(current_wall_link)
            
            cache_path = self._get_cache_path(file_name)
            if os.path.exists(cache_path):
                os.symlink(cache_path, current_wall_link)
            elif os.path.exists(tmp_thumb):
                stable_tmp = os.path.expanduser(f"~/.cache/ax-shell/last_video_thumb.jpg")
                shutil.copy(tmp_thumb, stable_tmp)
                os.symlink(stable_tmp, current_wall_link)
            
            if self.matugen_enabled and os.path.exists(tmp_thumb):
                 exec_shell_command_async(f'matugen image "{tmp_thumb}" -t {selected_scheme}')

        else:
            print(f"Setting image (awww): {file_name}")
            
            # --- ИСПОЛЬЗУЕМ AWWW ---
            # Убедимся, что init запущен (если нет - запустим)
            # awww init не падает, если уже запущен
            subprocess.run(["awww", "init"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # Ставим картинку
            # awww img <path> --transition-type wipe --transition-fps 60 ...
            subprocess.Popen(
                ["awww", "img", full_path, "--transition-type", "wipe", "--transition-fps", "60", "--transition-step", "2"],
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL
            )
            
            # Симлинк на оригинал
            if os.path.exists(current_wall_link) or os.path.islink(current_wall_link):
                os.remove(current_wall_link)
            os.symlink(full_path, current_wall_link)
            
            # Matugen
            if self.matugen_enabled:
                exec_shell_command_async(f'matugen image "{full_path}" -t {selected_scheme}')


    def on_wallpaper_selected(self, iconview, path):
        model = iconview.get_model()
        file_name = model[path][1]
        self._apply_wallpaper(file_name)

    def set_random_wallpaper(self, widget, external=False):
        if not self.files:
            return
        file_name = random.choice(self.files)
        self._apply_wallpaper(file_name)
        self.randomize_dice_icon()
        
        if external:
             full_path = os.path.join(data.WALLPAPERS_DIR, file_name)
             exec_shell_command_async(
                f"notify-send '🎲 Wallpaper' 'Setting random wallpaper' -a '{data.APP_NAME_CAP}' -i '{full_path}'"
            )

    def _get_cache_path(self, file_name: str) -> str:
        file_hash = hashlib.md5(file_name.encode("utf-8")).hexdigest()
        return os.path.join(self.CACHE_DIR, f"{file_hash}.png")

    @staticmethod
    def _is_image_or_video(file_name: str) -> bool:
        return file_name.lower().endswith(
            (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", 
            ".mp4", ".mkv", ".webm", ".mov")
        )

    # --- UI Helpers ---
    def on_map(self, widget):
        self.custom_color_selector_box.set_visible(not self.matugen_enabled)

    def on_switch_toggled(self, switch, gparam):
        self.matugen_enabled = switch.get_active()
        self.custom_color_selector_box.set_visible(not self.matugen_enabled)
        with open(data.MATUGEN_STATE_FILE, "w") as f:
            f.write(str(self.matugen_enabled))

    def on_apply_color_clicked(self, button):
        hue = self.hue_slider.get_value()
        hex_color = self.hsl_to_rgb_hex(hue)
        selected_scheme = self.scheme_dropdown.get_active_id()
        exec_shell_command_async(f'matugen color hex "{hex_color}" -t {selected_scheme}')

    def hsl_to_rgb_hex(self, h: float, s: float = 1.0, l: float = 0.5) -> str:
        hue = h / 360.0
        r, g, b = colorsys.hls_to_rgb(hue, l, s)
        return f"#{int(r*255):02X}{int(g*255):02X}{int(b*255):02X}"
        
    def arrange_viewport(self, query: str = ""):
        model = self.viewport.get_model()
        model.clear()
        for pixbuf, name in self.thumbnails:
            if query.lower() in name.lower():
                model.append([pixbuf, name])
                
    def on_search_entry_key_press(self, widget, event):
        return False # Simplified
        
    def randomize_dice_icon(self):
        icons_list = [icons.dice_1, icons.dice_2, icons.dice_3, icons.dice_4, icons.dice_5, icons.dice_6]
        self.random_wall.get_child().set_markup(random.choice(icons_list))

    def randomize_dice_icon(self):
        icons_list = [
            icons.dice_1,
            icons.dice_2,
            icons.dice_3,
            icons.dice_4,
            icons.dice_5,
            icons.dice_6,
        ]
        label = self.random_wall.get_child()
        if isinstance(label, Label):
             label.set_markup(random.choice(icons_list))

    def on_scheme_changed(self, combo):
        # Метод-заглушка для обработки смены схемы (можно добавить логику сохранения)
        pass

    def on_search_entry_key_press(self, widget, event):
        # Упрощенная навигация по списку через Enter в поиске
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
             model = self.viewport.get_model()
             if len(model) > 0:
                 # Если нажали Enter, выбираем первый результат поиска
                 path = Gtk.TreePath.new_from_indices([0])
                 self.viewport.select_path(path)
                 self.on_wallpaper_selected(self.viewport, path)
             return True
        return False
