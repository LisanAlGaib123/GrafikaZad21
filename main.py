#!/usr/bin/env python3
"""
PPM (P3/P6) + JPEG viewer and converter.

Funkcje:
- Wczytywanie P3 (ASCII) i P6 (binary) PPM (bez użycia gotowych bibliotek PPM).
- Wydajne blokowe wczytywanie danych binarnych.
- Obsługa maxval > 255 (2 bajty, big-endian) oraz skalowanie liniowe do 0-255.
- Wczytywanie i zapisywanie JPEG (Pillow). Użytkownik wybiera jakość (kompresję).
- GUI (tkinter) z powiększaniem, przesuwaniem, oraz pokazywaniem wartości R,G,B piksela.
- Obsługa błędów i komunikaty.

Autor: przykładowe rozwiązanie dla zadania uczelnianego.
"""

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
from PIL import Image, ImageTk, ImageOps
import io
import os
import struct
import time

# --------------------------
# PPM parsing utilities
# --------------------------

class PPMFormatError(Exception):
    pass

def _read_header_tokens(f):
    """
    Czyta linie z pliku binarnego f (opened in 'rb') i zwraca listę tokenów nagłówka
    (po magic, width, height, maxval). Pomija komentarze (# do końca linii).
    Uwaga: pozostawia wskaźnik pliku tuż za linią z maxval (czyli gotowe do czytania danych P6).
    """
    tokens = []
    # First line is magic (we assume caller już przeczytał magic jeśli chce; ale tu pobierzemy wszystko)
    # We'll read lines until we have 4 tokens: magic, width, height, maxval
    while len(tokens) < 4:
        line = f.readline()
        if not line:
            break
        # decode as ascii ignoring errors, but keep bytes for safety
        try:
            s = line.decode('ascii')
        except Exception:
            s = line.decode('latin1')
        # remove comments
        if '#' in s:
            s = s.split('#', 1)[0]
        # split whitespace
        parts = s.split()
        tokens.extend(parts)
    if len(tokens) < 4:
        raise PPMFormatError("Nie udało się odczytać nagłówka PPM (brak wymaganych pól).")
    return tokens[:4]  # magic, width, height, maxval

def timed(func):
    """Dekorator do pomiaru czasu wykonania funkcji (do debugowania)."""
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        result = func(*args, **kwargs)
        end = time.perf_counter()
        print(f"[DEBUG] {func.__name__} wykonano w {end - start:.3f} sekundy.")
        return result
    return wrapper

@timed
def read_ppm(path):
    """
    Wczytuje plik PPM (P3 lub P6).
    Zwraca obiekt PIL.Image (mode='RGB').

    Rzuca PPMFormatError lub IOError w przypadku błędów.
    """
    with open(path, 'rb') as f:
        # read magic (first token, e.g. b'P6' or b'P3')
        first = f.readline()
        if not first:
            raise PPMFormatError("Pusty plik.")
        try:
            magic = first.decode('ascii').strip()
        except Exception:
            magic = first.decode('latin1').strip()
        if magic not in ('P3', 'P6'):
            raise PPMFormatError(f"Niedozwolony magic header: {magic}. Oczekiwano P3 lub P6.")

        # --- P3 (ASCII) parsing (streamowane, zachowujemy nadmiarowe tokeny z linii nagłówka) ---
        if magic == 'P3':
            # Wczytaj cały tekst po magicu i nagłówku do jednego stringa
            content = f.read().decode('ascii', errors='ignore')
            # Usuń komentarze (# do końca linii)
            lines = []
            for line in content.splitlines():
                if '#' in line:
                    line = line.split('#', 1)[0]
                if line.strip():
                    lines.append(line)
            tokens = ' '.join(lines).split()
            # Pierwsze 3 to width, height, maxval
            if len(tokens) < 3:
                raise PPMFormatError("Brak nagłówka (width height maxval)")
            width = int(tokens[0])
            height = int(tokens[1])
            maxval = int(tokens[2])
            data_tokens = tokens[3:]
            expected = width * height * 3
            if len(data_tokens) != expected:
                raise PPMFormatError(f"Nieoczekiwana liczba próbek: {len(data_tokens)} != {expected}")
            # Konwersja i skalowanie
            samples = list(map(int, data_tokens))
            if maxval != 255:
                scale = 255.0 / maxval
                samples = bytes(int(round(v * 255.0 / maxval)) if maxval != 255 else v for v in samples)
            data = bytes(samples)
            img = Image.frombytes('RGB', (width, height), data)
            return img


        # --- P6 (binary) parsing (bez zmian) ---
        else:
            # read remaining header tokens (width height maxval) - this branch shouldn't happen because we handled magic earlier,
            # but we need to parse header in case width/height/maxval were not on separate lines; we'll reuse similar logic:
            # We assume after first.readline() (magic) we are at next line; we need width,height,maxval.
            # The original implementation read again; we'll implement safe header read then binary read.
            # Go back to beginning of file and reparse safely:
            f.seek(0)
            # Read first token (magic) and then stream tokens skipping comments until we have 4 tokens total
            tokens = []
            while len(tokens) < 4:
                line = f.readline()
                if not line:
                    break
                try:
                    s = line.decode('ascii')
                except Exception:
                    s = line.decode('latin1')
                if '#' in s:
                    s = s.split('#', 1)[0]
                parts = s.split()
                if parts:
                    tokens.extend(parts)
            if len(tokens) < 4:
                raise PPMFormatError("Nie udało się odczytać nagłówka P6.")
            # tokens[0] = magic, tokens[1]=width, tokens[2]=height, tokens[3]=maxval
            try:
                width = int(tokens[1])
                height = int(tokens[2])
                maxval = int(tokens[3])
            except Exception as e:
                raise PPMFormatError("Błąd parsowania width/height/maxval (P6).") from e
            if width <= 0 or height <= 0:
                raise PPMFormatError("Nieprawidłowe wymiary obrazu.")
            if not (1 <= maxval <= 65535):
                raise PPMFormatError("maxval poza zakresem (1..65535).")
            # After reading lines with readline(), file pointer stoi za linią zawierającą maxval => gotowe do czytania bajtów.
            if maxval < 256:
                bps = 1
            else:
                bps = 2
            total_samples = width * height * 3
            total_bytes = total_samples * bps
            remaining = total_bytes
            buf = bytearray()
            block_size = 64 * 1024
            while remaining > 0:
                to_read = min(block_size, remaining)
                chunk = f.read(to_read)
                if not chunk:
                    break
                buf.extend(chunk)
                remaining -= len(chunk)
            if len(buf) != total_bytes:
                raise PPMFormatError(f"Nieoczekiwana liczba bajtów pikseli w P6: odczytano {len(buf)}, oczekiwano {total_bytes}")
            if bps == 1:
                if maxval == 255:
                    data = bytes(buf)
                else:
                    scale = 255.0 / maxval
                    data = bytes([int(round(b * scale)) for b in buf])
                img = Image.frombytes('RGB', (width, height), data)
                return img
            else:
                scale = 255.0 / maxval
                samples8 = bytearray(total_samples)
                mv = memoryview(buf)
                idx_out = 0
                for i in range(0, total_bytes, 2):
                    hi = mv[i]
                    lo = mv[i+1]
                    val = (hi << 8) | lo
                    if val < 0:
                        val = 0
                    elif val > maxval:
                        val = maxval
                    samples8[idx_out] = int(round(val * scale))
                    idx_out += 1
                img = Image.frombytes('RGB', (width, height), bytes(samples8))
                return img


# --------------------------
# JPEG helpers
# --------------------------

def read_image_general(path):
    """
    Wczytuje plik - jeśli to PPM (rozpoznanie po magicu lub rozszerzeniu), używa read_ppm,
    w przeciwnym razie próbuje Pillow (JPEG itp).
    Zwraca PIL.Image w trybie RGB.
    """
    # quick test: open file and read first two bytes
    with open(path, 'rb') as f:
        head = f.read(2)
    try:
        head_str = head.decode('ascii', errors='ignore')
    except:
        head_str = ''
    if head_str in ('P3', 'P6'):
        # use our PPM reader
        return read_ppm(path)
    # else try Pillow for JPEG and other formats
    try:
        im = Image.open(path)
        im = im.convert('RGB')
        return im
    except Exception as e:
        raise IOError(f"Nie można wczytać pliku jako PPM ani obraz przez Pillow: {e}")

def save_as_jpeg(image, path, quality=85):
    """
    Zapisuje PIL.Image jako JPEG z wybraną jakością (1..95 typowo).
    """
    if not (1 <= quality <= 95):
        raise ValueError("Quality musi być w zakresie 1..95.")
    image.save(path, format='JPEG', quality=quality, optimize=True)

# --------------------------
# GUI
# --------------------------

class ImageViewer(tk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.master.title("PPM (P3/P6) & JPEG Viewer")
        self.pack(fill=tk.BOTH, expand=True)
        # state
        self.image = None            # PIL original image
        self.display_image = None    # PIL resized for display
        self.tkimage = None
        self.zoom = 1.0
        self.offset_x = 0
        self.offset_y = 0
        self.drag_start = None
        # build UI
        self.build_ui()

    def build_ui(self):
        toolbar = tk.Frame(self)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        btn_open = tk.Button(toolbar, text="Otwórz...", command=self.open_file)
        btn_open.pack(side=tk.LEFT, padx=4, pady=4)

        btn_save_jpeg = tk.Button(toolbar, text="Zapisz jako JPEG...", command=self.save_jpeg)
        btn_save_jpeg.pack(side=tk.LEFT, padx=4, pady=4)

        tk.Label(toolbar, text="Zoom:").pack(side=tk.LEFT, padx=(8,0))
        self.zoom_var = tk.DoubleVar(value=1.0)
        zoom_scale = tk.Scale(toolbar, variable=self.zoom_var, from_=0.1, to=8.0, resolution=0.1, orient=tk.HORIZONTAL, command=self.on_zoom_change, length=200)
        zoom_scale.pack(side=tk.LEFT, padx=4)

        btn_fit = tk.Button(toolbar, text="Dopasuj okno", command=self.fit_to_window)
        btn_fit.pack(side=tk.LEFT, padx=4)

        info_frame = tk.Frame(self)
        info_frame.pack(side=tk.BOTTOM, fill=tk.X)
        self.info_label = tk.Label(info_frame, text="Brak obrazu", anchor='w')
        self.info_label.pack(side=tk.LEFT, padx=6, pady=4)

        # canvas
        self.canvas = tk.Canvas(self, bg='black', cursor='cross')
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<ButtonPress-1>", self.on_button_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_button_release)
        self.canvas.bind("<Motion>", self.on_mouse_move)
        self.canvas.bind("<Configure>", self.on_resize)

    def open_file(self):
        filepath = filedialog.askopenfilename(title="Wybierz plik (PPM P3/P6 lub JPEG)", filetypes=[("Images", "*.ppm *.PPM *.jpg *.jpeg *.JPG *.JPEG"), ("All files", "*.*")])
        if not filepath:
            return
        try:
            img = read_image_general(filepath)
        except Exception as e:
            messagebox.showerror("Błąd", f"Nie można wczytać pliku:\n{e}")
            return
        self.image = img
        self.zoom = 1.0
        self.offset_x = 0
        self.offset_y = 0
        self.zoom_var.set(1.0)
        self.update_display_image()
        self.info_label.config(text=f"Wczytano: {os.path.basename(filepath)}  ({self.image.width}x{self.image.height})")

    def save_jpeg(self):
        if self.image is None:
            messagebox.showinfo("Brak obrazu", "Najpierw otwórz obraz.")
            return
        # ask quality
        q = simpledialog.askinteger("JPG jakość", "Wybierz jakość JPEG (1-95, większe = lepsza jakość, mniejsza = silniejsza kompresja):", initialvalue=85, minvalue=1, maxvalue=95)
        if q is None:
            return
        path = filedialog.asksaveasfilename(defaultextension=".jpg", filetypes=[("JPEG", "*.jpg;*.jpeg")], title="Zapisz jako JPEG")
        if not path:
            return
        try:
            save_as_jpeg(self.image, path, quality=q)
            messagebox.showinfo("Zapisano", f"Zapisano obraz do {path} (jakość={q}).")
        except Exception as e:
            messagebox.showerror("Błąd zapisu", f"Nie udało się zapisać pliku JPEG:\n{e}")

    def update_display_image(self):
        if self.image is None:
            return
        # compute displayed size
        w = int(round(self.image.width * self.zoom))
        h = int(round(self.image.height * self.zoom))
        if w < 1: w = 1
        if h < 1: h = 1
        # resize with Pillow - good default resampling
        try:
            self.display_image = self.image.resize((w, h), resample=Image.NEAREST if self.zoom>=1.0 else Image.BILINEAR)
        except Exception:
            self.display_image = self.image.copy()
        # create PhotoImage
        self.tkimage = ImageTk.PhotoImage(self.display_image)
        self.redraw_canvas()

    def redraw_canvas(self):
        self.canvas.delete("all")
        if self.tkimage is None:
            return
        # compute center or offset
        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        # place image at offset_x, offset_y relative to canvas center
        x = (canvas_w // 2) + int(self.offset_x)
        y = (canvas_h // 2) + int(self.offset_y)
        # remember image id to allow moving
        self.canvas_image_id = self.canvas.create_image(x, y, image=self.tkimage)
        # add border
        self.canvas.config(scrollregion=self.canvas.bbox(tk.ALL))

    def on_zoom_change(self, val):
        try:
            self.zoom = float(val)
        except:
            self.zoom = 1.0
        self.update_display_image()

    def fit_to_window(self):
        if self.image is None:
            return
        canvas_w = max(1, self.canvas.winfo_width())
        canvas_h = max(1, self.canvas.winfo_height())
        # compute scale to fit
        scale_x = canvas_w / self.image.width
        scale_y = canvas_h / self.image.height
        new_zoom = min(scale_x, scale_y) * 0.95  # leave small margin
        if new_zoom <= 0:
            new_zoom = 1.0
        self.zoom = new_zoom
        self.zoom_var.set(self.zoom)
        self.offset_x = 0
        self.offset_y = 0
        self.update_display_image()

    def on_button_press(self, event):
        self.drag_start = (event.x, event.y)

    def on_drag(self, event):
        if not self.drag_start:
            return
        dx = event.x - self.drag_start[0]
        dy = event.y - self.drag_start[1]
        self.offset_x += dx
        self.offset_y += dy
        self.drag_start = (event.x, event.y)
        self.redraw_canvas()

    def on_button_release(self, event):
        self.drag_start = None

    def on_mouse_move(self, event):
        if self.image is None or self.display_image is None:
            return
        # compute image coordinates corresponding to mouse
        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        # image top-left on canvas:
        img_w, img_h = self.display_image.width, self.display_image.height
        img_center_x = (canvas_w // 2) + int(self.offset_x)
        img_center_y = (canvas_h // 2) + int(self.offset_y)
        img_left = img_center_x - img_w // 2
        img_top = img_center_y - img_h // 2
        # mouse over image?
        mx = event.x - img_left
        my = event.y - img_top
        if 0 <= mx < img_w and 0 <= my < img_h:
            # map to original image coords
            ox = int(mx / self.zoom)
            oy = int(my / self.zoom)
            # clamp
            ox = min(max(ox, 0), self.image.width - 1)
            oy = min(max(oy, 0), self.image.height - 1)
            try:
                r,g,b = self.image.getpixel((ox, oy))
            except Exception:
                r,g,b = (0,0,0)
            self.info_label.config(text=f"X={ox} Y={oy}  R={r} G={g} B={b}   Zoom={self.zoom:.2f}")
        else:
            self.info_label.config(text=f"Zoom={self.zoom:.2f}  Brak obrazu pod kursorem")

    def on_resize(self, event):
        # optionally auto-fit on large resize? We'll just redraw.
        if self.image is None:
            return
        # keep current zoom; just redraw
        self.redraw_canvas()

# --------------------------
# main
# --------------------------

def main():
    root = tk.Tk()
    root.geometry("1000x700")
    app = ImageViewer(root)
    root.mainloop()

if __name__ == "__main__":
    main()
