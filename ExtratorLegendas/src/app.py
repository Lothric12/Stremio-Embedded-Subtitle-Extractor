"""Windows GUI for selective subtitle extraction."""
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from extractor import RangeSource, Matroska, ExtractionError, Cancelled, render, save_atomic, MIB


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Extrator de legendas 1.1 • Stremio')
        self.geometry('980x780')
        self.minsize(900, 720)
        self.configure(bg='#101422')
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.media = None
        self.source = None
        self.analyzed_url = None
        self.last_saved = None
        self.last_update = 0
        self.closing = False
        self.job_started = None
        self.destination = None
        self.current_fraction = 0
        self.current_count = 0
        self.url = tk.StringVar()
        self.limit = tk.StringVar(value='128')
        self.mode = tk.StringVar(value='Equilibrado')
        self.format = tk.StringVar(value='SRT (compatível com players)')
        self.status = tk.StringVar(value='Pronto. Cole o link do stream para começar.')
        self.stats = tk.StringVar(value='Nenhum vídeo será salvo em disco.')
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('.', font=('Segoe UI', 10), background='#101422', foreground='#e8eaf4')
        style.configure('TFrame', background='#101422')
        style.configure('TLabel', background='#101422')
        style.configure('Title.TLabel', font=('Segoe UI Semibold', 23), foreground='#ffffff')
        style.configure('Muted.TLabel', foreground='#aeb8d0')
        style.configure('TButton', background='#303b61', padding=(12, 9), borderwidth=0)
        style.map('TButton', background=[('active', '#495a91'), ('disabled', '#20283f')],
                  foreground=[('disabled', '#737d98')])
        style.configure('Accent.TButton', background='#6257d8', foreground='white')
        style.map('Accent.TButton', background=[('active', '#796ded'), ('disabled', '#30334d')])
        style.configure('TEntry', fieldbackground='#20283f', foreground='white', padding=8)
        style.configure('TCombobox', fieldbackground='#20283f', foreground='white', padding=6)
        style.map('TCombobox', fieldbackground=[('readonly', '#20283f')],
                  foreground=[('readonly', 'white')])
        style.configure('Treeview', background='#192136', fieldbackground='#192136',
                        foreground='#e8eaf4', rowheight=34, borderwidth=0)
        style.configure('Treeview.Heading', background='#28324e', foreground='white', padding=8)
        style.map('Treeview', background=[('selected', '#5549b2')])
        style.configure('Horizontal.TProgressbar', background='#9485ff', troughcolor='#222b43')
        self.build()
        self.protocol('WM_DELETE_WINDOW', self.on_close)
        self.after(100, self.poll)

    def build(self):
        f = ttk.Frame(self, padding=26)
        f.pack(fill='both', expand=True)
        ttk.Label(f, text='Sua legenda, fora do vídeo.', style='Title.TLabel').pack(anchor='w')
        ttk.Label(f, text='Extraia faixas Embedded de streams MKV/WebM e salve em SRT ou ASS/SSA.',
                  style='Muted.TLabel').pack(anchor='w', pady=(6, 19))
        ttk.Label(f, text='1   No player do Stremio, use “Copiar link do stream / Copy stream link”.').pack(anchor='w')
        row = ttk.Frame(f)
        row.pack(fill='x', pady=(9, 14))
        self.entry = ttk.Entry(row, textvariable=self.url)
        self.entry.pack(side='left', fill='x', expand=True, padx=(0, 8))
        self.paste_button = ttk.Button(row, text='Colar', command=self.paste)
        self.paste_button.pack(side='left', padx=(0, 8))
        self.analyze_button = ttk.Button(row, text='Analisar faixas', style='Accent.TButton', command=self.analyze)
        self.analyze_button.pack(side='left')
        opts = ttk.Frame(f)
        opts.pack(fill='x', pady=(0, 18))
        ttk.Label(opts, text='Limite de leitura:').pack(side='left')
        self.limit_entry = ttk.Entry(opts, textvariable=self.limit, width=6)
        self.limit_entry.pack(side='left', padx=7)
        ttk.Label(opts, text='MiB   •   Modo:').pack(side='left')
        self.mode_combo = ttk.Combobox(opts, textvariable=self.mode, values=['Equilibrado', 'Econômico'],
                                       state='readonly', width=14)
        self.mode_combo.pack(side='left', padx=7)
        ttk.Label(opts, text='Teto adicional: 35% do stream', style='Muted.TLabel').pack(side='right')
        ttk.Label(f, text='2   Selecione a faixa de legenda.').pack(anchor='w', pady=(0, 8))
        table = ttk.Frame(f)
        table.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(table, columns=('language', 'name', 'codec', 'status'), show='headings', height=4)
        for col, label, width in [('language', 'Idioma', 85), ('name', 'Nome da faixa', 280),
                                  ('codec', 'Formato', 140), ('status', 'Disponibilidade', 150)]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, minwidth=65)
        bar = ttk.Scrollbar(table, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        bar.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', lambda event: self.selection_changed())
        row = ttk.Frame(f)
        row.pack(fill='x', pady=(15, 14))
        self.format_combo = ttk.Combobox(row, textvariable=self.format, state='readonly',
                                        values=['SRT (compatível com players)'], width=29)
        self.format_combo.pack(side='left')
        self.extract_button = ttk.Button(row, text='Salvar legenda…', style='Accent.TButton',
                                         command=self.extract, state='disabled')
        self.extract_button.pack(side='left', padx=10)
        self.cancel_button = ttk.Button(row, text='Cancelar', command=self.cancel_job, state='disabled')
        self.cancel_button.pack(side='left')
        self.folder_button = ttk.Button(row, text='Abrir pasta', command=self.open_folder, state='disabled')
        self.folder_button.pack(side='right')
        self.progress = ttk.Progressbar(f, maximum=100)
        self.progress.pack(fill='x')
        ttk.Label(f, textvariable=self.status, wraplength=900).pack(anchor='w', pady=(9, 4))
        ttk.Label(f, textvariable=self.stats, style='Muted.TLabel', wraplength=900).pack(anchor='w')
        ttk.Label(f, text='Lê pequenos trechos do stream; não baixa o arquivo inteiro. Pode demorar em servidores lentos.\n'
                  'Mantenha o Stremio aberto. Em torrents, o Stremio pode baixar/cachear blocos adicionais.',
                  style='Muted.TLabel', wraplength=820).pack(anchor='w', pady=(15, 0))

    def paste(self):
        try:
            self.url.set(self.clipboard_get().strip())
        except tk.TclError:
            messagebox.showinfo('Área de transferência', 'Copie o link do stream no Stremio primeiro.')

    def selected(self):
        selection = self.tree.selection()
        return self.media.tracks[int(selection[0])] if selection and self.media else None

    def selection_changed(self):
        t = self.selected()
        if not self.busy:
            self.extract_button.configure(state='normal' if t and t.usable else 'disabled')
        formats = ['SRT (compatível com players)']
        if t and t.codec in ('S_TEXT/ASS', 'S_TEXT/SSA'):
            formats.append(('ASS' if t.codec.endswith('/ASS') else 'SSA') + ' (preservar estilos)')
        self.format_combo.configure(values=formats)
        if self.format.get() not in formats:
            self.format.set(formats[0])
        if t and not t.usable:
            self.status.set(t.unsupported)

    def set_busy(self, busy):
        self.busy = busy
        for widget in (self.entry, self.paste_button, self.analyze_button, self.limit_entry):
            widget.configure(state='disabled' if busy else 'normal')
        for widget in (self.mode_combo, self.format_combo):
            widget.configure(state='disabled' if busy else 'readonly')
        self.cancel_button.configure(state='normal' if busy else 'disabled')
        self.extract_button.configure(state='disabled')
        if not busy:
            self.selection_changed()

    def run_job(self, function):
        self.cancel = threading.Event()
        self.job_started = time.monotonic()
        self.current_fraction = 0
        self.current_count = 0
        if self.source:
            self.source.cancel = self.cancel
        self.set_busy(True)
        def work():
            try:
                function()
            except Cancelled as error:
                self.events.put(('cancelled', str(error)))
            except ExtractionError as error:
                self.events.put(('error', str(error)))
            except Exception as error:
                # No URLs/tokens in error dialogs or logs.
                self.events.put(('error', 'Não foi possível concluir a operação (' + type(error).__name__ + '). '
                                         'Verifique o stream e tente novamente.'))
            finally:
                self.events.put(('done', None))
        threading.Thread(target=work, daemon=True).start()

    def cancel_job(self):
        self.cancel.set()
        self.status.set('Cancelando… Aguardando encerrar a leitura atual (até 20 s).')

    def analyze(self):
        url = self.url.get().strip()
        try:
            limit = int(self.limit.get())
            if not 1 <= limit <= 2048:
                raise ValueError()
        except ValueError:
            messagebox.showerror('Limite inválido', 'Digite um limite entre 1 e 2048 MiB.')
            return
        if self.source:
            self.source.close()
        self.source = None
        self.media = None
        self.analyzed_url = None
        self.tree.delete(*self.tree.get_children())
        self.progress['value'] = 0
        self.status.set('Lendo o cabeçalho e procurando faixas de legenda…')
        prefetch = 512 if self.mode.get() == 'Econômico' else 2048
        def job():
            source = RangeSource(url, limit, self.cancel)
            try:
                media = Matroska(source, prefetch)
                media.inspect()
            except Exception:
                source.close()
                raise
            self.events.put(('analyzed', (source, media, url)))
        self.run_job(job)

    def extract(self):
        t = self.selected()
        if not t or not t.usable:
            return
        if self.url.get().strip() != self.analyzed_url:
            messagebox.showinfo('Link alterado', 'Clique em Analisar faixas para usar o novo link.')
            return
        try:
            limit = int(self.limit.get())
            if not 1 <= limit <= 2048:
                raise ValueError()
        except ValueError:
            messagebox.showerror('Limite inválido', 'Digite um limite entre 1 e 2048 MiB.')
            return
        self.source.limit = min(limit * MIB, int(self.source.size * 0.35))
        self.media.prefetch = 512 if self.mode.get() == 'Econômico' else 2048
        extension = self.format.get().split()[0].lower()
        language = ''.join(c for c in t.language if c.isalnum() or c in '-_') or 'und'
        path = filedialog.asksaveasfilename(title='Salvar legenda', defaultextension='.' + extension,
                   initialfile=f'legenda.{language}.{extension}',
                   filetypes=[('Legenda ' + extension.upper(), '*.' + extension)])
        if not path:
            return
        self.destination = path
        self.progress['value'] = 0
        self.status.set('Extraindo a faixa selecionada…')
        def job():
            cues = self.media.extract(t, self.report)
            self.source.check()
            result = render(cues, t, extension)
            self.source.check()
            save_atomic(path, result)
            self.events.put(('saved', (path, len(cues))))
        self.run_job(job)

    def report(self, fraction, count):
        now = time.monotonic()
        if now - self.last_update > 0.2:
            self.last_update = now
            self.events.put(('progress', (fraction, count, self.source.transferred, self.source.requests)))

    def poll(self):
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == 'analyzed':
                    self.source, self.media, self.analyzed_url = value
                    for i, t in enumerate(self.media.tracks):
                        self.tree.insert('', 'end', iid=str(i), values=(t.language,
                            t.name or ('Forçada' if t.forced else 'Faixa ' + str(t.number)),
                            t.codec.replace('S_TEXT/', ''), 'Disponível' if t.usable else 'Não suportada'))
                    preferred = [i for i, t in enumerate(self.media.tracks) if t.usable and t.language.startswith(('pt', 'por'))]
                    choices = preferred or [i for i, t in enumerate(self.media.tracks) if t.usable]
                    if choices:
                        self.tree.selection_set(str(choices[0]))
                    self.status.set(f'{len(self.media.tracks)} faixa(s) encontrada(s). Selecione uma e salve a legenda.')
                    self.update_stats()
                elif event == 'progress':
                    fraction, count, transferred, requests = value
                    if fraction is not None:
                        self.progress['value'] = fraction * 100
                        self.current_fraction = fraction
                        self.current_count = count
                    self.update_stats()
                elif event == 'saved':
                    self.last_saved, count = value
                    self.progress['value'] = 100
                    self.status.set(f'Legenda salva! {count} eventos extraídos.')
                    self.folder_button.configure(state='normal')
                    self.update_stats()
                    if not self.closing:
                        messagebox.showinfo('Concluído', 'Legenda salva em:\n' + self.last_saved)
                elif event in ('error', 'cancelled'):
                    self.status.set(value)
                    self.update_stats()
                    if event == 'error' and not self.closing:
                        messagebox.showerror('Não foi possível extrair', value)
                elif event == 'done':
                    self.set_busy(False)
                    if self.closing:
                        if self.source:
                            self.source.close()
                        self.destroy()
                        return
        except queue.Empty:
            pass
        if self.busy and self.media and self.source:
            elapsed = time.monotonic() - self.job_started
            waits = [p.waiting_since for p in self.source.peers if p.waiting_since is not None]
            wait_seconds = time.monotonic() - min(waits) if waits else 0
            if not self.cancel.is_set():
                suffix = f' • aguardando Stremio há {wait_seconds:.0f}s' if wait_seconds >= 2 else ''
                self.status.set(f'{self.media.method}: {self.current_fraction:.2%} • '
                                f'{self.current_count} falas • {int(elapsed)}s{suffix}')
            self.update_stats()
        self.after(100, self.poll)

    def update_stats(self):
        if self.source:
            s = self.source
            self.stats.set(f'Lidos: {s.transferred / MIB:.2f} MiB de {s.size / MIB:.1f} MiB '
                           f'({s.transferred / s.size:.3%}) • {s.requests} requisições'
                           + ('\nDestino ao concluir: ' + self.destination if self.destination else ''))

    def open_folder(self):
        if self.last_saved:
            os.startfile(str(Path(self.last_saved).parent))

    def on_close(self):
        if self.busy:
            self.closing = True
            self.cancel.set()
            self.status.set('Cancelando e fechando… A conexão pode levar até 20 segundos para responder.')
        else:
            if self.source:
                self.source.close()
            self.destroy()


if __name__ == '__main__':
    App().mainloop()
