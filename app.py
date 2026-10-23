from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Qt
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QProgressBar,
    QLineEdit,
    QWidget,
)

TIME_RANGE_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?)\s*"
    r"(?:->|→|➜|←|<-|[-–—])\s*"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?)"
)
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
SCENE_LABEL_RE = re.compile(r"^\s*(?:مشهد|المشهد)\s*\d+\s*[:.)-]?\s*", re.IGNORECASE)

MODEL_ID = "mohammedaly22/VoiceTut-TTS"
SPEAKERS = [
    ("Ahmed", "أحمد — شبابي"),
    ("Ali", "علي — شبابي"),
    ("Hassan", "حسن — رجالي"),
    ("Karim", "كريم — شبابي"),
    ("Mahmoud", "محمود — رجالي"),
    ("Mohammed", "محمد — رجالي"),
    ("Mostafa", "مصطفى — رجالي"),
    ("Ramy", "رامي — شبابي"),
    ("Shaker", "شاكر — رجالي"),
    ("Tareq", "طارق — رجالي"),
    ("Yehia", "يحيى — رجالي"),
    ("Youssef", "يوسف — شبابي"),
    ("Hadeer", "هدير — نسائي"),
    ("Hanan", "حنان — نسائي"),
    ("Mariam", "مريم — نسائي"),
    ("Rana", "رنا — نسائي"),
    ("Sarah", "سارة — نسائي"),
    ("Yasmin", "ياسمين — نسائي"),
    ("Yara", "يارا — نسائي"),
]


@dataclass
class Scene:
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return self.end - self.start


def bundled_tool(name: str) -> str:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    candidate = base / "bin" / (name + (".exe" if sys.platform == "win32" else ""))
    return str(candidate) if candidate.exists() else name


def parse_time(value: str) -> float:
    parts = [int(x.translate(ARABIC_DIGITS)) for x in value.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_scenes(raw: str) -> list[Scene]:
    # نعالج اتجاه RTL وعلامات التنسيق غير المرئية التي قد يضيفها Word/المتصفح.
    lines = [
        line.translate(ARABIC_DIGITS).replace("\u200f", "").replace("\u200e", "").strip()
        for line in raw.splitlines()
    ]
    scenes: list[Scene] = []
    pending: Scene | None = None
    pending_text: list[str] = []

    def finish_pending() -> None:
        nonlocal pending, pending_text
        if pending is None:
            return
        text = " ".join(x.strip(" |:—-–>→←") for x in pending_text if x.strip())
        if not text:
            raise ValueError(
                "وجدت توقيتًا بدون نص منطوق. اكتب النص في نفس السطر أو في السطر التالي."
            )
        scenes.append(Scene(pending.start, pending.end, text))
        pending = None
        pending_text = []

    for line_no, line in enumerate(lines, 1):
        if not line or line.startswith("#"):
            continue
        match = TIME_RANGE_RE.search(line)
        if not match:
            # في تنسيق «مشهد 01: التوقيت» يأتي النص في السطر التالي؛
            # وما قبل أول توقيت (العنوان/الملاحظات) يتم تجاهله بأمان.
            if pending is not None:
                pending_text.append(SCENE_LABEL_RE.sub("", line).strip())
            continue

        finish_pending()
        start = parse_time(match.group("start"))
        end = parse_time(match.group("end"))
        start, end = min(start, end), max(start, end)
        suffix = line[match.end():].strip(" |:—-–>→←")
        prefix = line[:match.start()].strip(" |:—-–>→←")
        inline = suffix if suffix else prefix
        inline = SCENE_LABEL_RE.sub("", inline).strip()
        # لو كان السطر مجرد «مشهد 01: 00:00 → 00:07»، انتظر النص التالي.
        if inline and not re.fullmatch(r"(?:مشهد|المشهد)\s*\d+", inline, re.IGNORECASE):
            pending_text = [inline]
        else:
            pending_text = []
        pending = Scene(start, end, "")

    finish_pending()
    if not scenes:
        raise ValueError(
            "لم أجد توقيتات مفهومة. الصيغة المقبولة مثلًا:\n"
            "مشهد 1: 00:00 → 00:07\nالنص المنطوق هنا"
        )
    for previous, current in zip(scenes, scenes[1:]):
        if current.start < previous.end:
            raise ValueError("التوقيتات متداخلة؛ راجع بداية كل مشهد")
    return scenes


def ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        [bundled_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def atempo_filter(value: float) -> str:
    filters: list[str] = []
    while value < 0.5:
        filters.append("atempo=0.5")
        value /= 0.5
    while value > 2.0:
        filters.append("atempo=2.0")
        value /= 2.0
    filters.append(f"atempo={value:.10f}")
    return ",".join(filters)


def retime_audio(source: Path, target: Path, seconds: float) -> None:
    actual = ffprobe_duration(source)
    if actual <= 0:
        raise RuntimeError("محرك الصوت أخرج ملفًا فارغًا")
    audio_filter = (
        f"{atempo_filter(actual / seconds)},apad=pad_dur={seconds:.6f},"
        f"atrim=duration={seconds:.6f},asetpts=N/SR/TB"
    )
    subprocess.run(
        [bundled_tool("ffmpeg"), "-y", "-i", str(source), "-filter:a", audio_filter,
         "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(target)],
        capture_output=True, text=True, check=True,
    )


class GenerateWorker(QObject):
    progress = Signal(int, str)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, scenes: list[Scene], speaker: str, output_path: Path):
        super().__init__()
        self.scenes = scenes
        self.speaker = speaker
        self.output_path = output_path

    def run(self) -> None:
        try:
            self.progress.emit(2, "تحميل محرك VoiceTut المصري أول مرة قد يستغرق وقتًا...")
            from voicetut_tts import VoiceTutTTS

            tts = VoiceTutTTS.from_pretrained(MODEL_ID)
            with tempfile.TemporaryDirectory(prefix="dishvoice_") as temp_dir:
                temp = Path(temp_dir)
                timed_files: list[Path] = []
                for index, scene in enumerate(self.scenes, 1):
                    self.progress.emit(
                        int((index - 1) / len(self.scenes) * 88) + 5,
                        f"توليد المشهد {index} من {len(self.scenes)} بصوت {self.speaker}...",
                    )
                    raw_wav = temp / f"raw_{index:03d}.wav"
                    timed_wav = temp / f"timed_{index:03d}.wav"
                    tts.synthesize(scene.text, speaker=self.speaker, output=str(raw_wav), num_step=32)
                    retime_audio(raw_wav, timed_wav, scene.duration)
                    timed_files.append(timed_wav)

                concat_file = temp / "concat.txt"
                concat_file.write_text(
                    "".join(f"file '{p.as_posix()}'\n" for p in timed_files), encoding="utf-8"
                )
                self.progress.emit(94, "دمج المشاهد وضبط الخط الزمني...")
                self.output_path.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    [bundled_tool("ffmpeg"), "-y", "-f", "concat", "-safe", "0", "-i",
                     str(concat_file), "-c:a", "pcm_s16le", str(self.output_path)],
                    capture_output=True, text=True, check=True,
                )
            self.progress.emit(100, "اكتمل التوليد بصوت مصري")
            self.done.emit(str(self.output_path))
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("DishVoice — مولّد فويس أوفر مصري قاهري")
        self.resize(1000, 720)
        self.thread: QThread | None = None
        self.worker: GenerateWorker | None = None

        central = QWidget()
        layout = QGridLayout(central)
        layout.setColumnStretch(1, 1)

        layout.addWidget(QLabel("الصوت المصري:"), 0, 0)
        self.speaker_combo = QComboBox()
        for code, label in SPEAKERS:
            self.speaker_combo.addItem(label, code)
        self.speaker_combo.setCurrentIndex(5)
        layout.addWidget(self.speaker_combo, 0, 1)
        info = QLabel("VoiceTut-TTS — 19 صوتًا مصريًا | النموذج يُنزّل تلقائيًا أول مرة")
        layout.addWidget(info, 0, 2)

        layout.addWidget(QLabel("ملف الإخراج (.wav):"), 1, 0)
        self.output_edit = QLineEdit(str(Path.home() / "Desktop" / "voiceover_egyptian.wav"))
        layout.addWidget(self.output_edit, 1, 1)
        output_button = QPushButton("اختيار")
        output_button.clicked.connect(self.choose_output)
        layout.addWidget(output_button, 1, 2)

        layout.addWidget(QLabel("المشاهد — كل سطر: البداية -> النهاية | النص"), 2, 0, 1, 3)
        self.script_edit = QPlainTextEdit()
        self.script_edit.setPlaceholderText(
            "مثال — الصق السكريبت هنا، ويمكن كتابة النص في السطر التالي للتوقيت:\n"
            "مشهد 1: 00:00 → 00:07\n"
            "عمرك سألت نفسك: إزاي غسالة الأطباق بتغسل كل ده؟"
        )
        layout.addWidget(self.script_edit, 3, 0, 1, 3)

        self.progress = QProgressBar()
        layout.addWidget(self.progress, 4, 0, 1, 2)
        self.status = QLabel("جاهز — VoiceTut-TTS مصري")
        layout.addWidget(self.status, 4, 2)

        self.generate_button = QPushButton("توليد الفويس أوفر المصري")
        self.generate_button.clicked.connect(self.generate)
        layout.addWidget(self.generate_button, 5, 0, 1, 3)
        self.setCentralWidget(central)

    def choose_output(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "حفظ الفويس أوفر", self.output_edit.text(), "WAV audio (*.wav)")
        if path:
            self.output_edit.setText(path)

    def generate(self) -> None:
        try:
            scenes = parse_scenes(self.script_edit.toPlainText())
            output = Path(self.output_edit.text().strip())
            if output.suffix.lower() != ".wav":
                raise ValueError("ملف الإخراج لازم يكون بصيغة .wav في النسخة الأولى")
        except Exception as exc:
            QMessageBox.warning(self, "راجع البيانات", str(exc))
            return

        self.generate_button.setEnabled(False)
        self.progress.setValue(0)
        self.status.setText("بدء التوليد...")
        self.thread = QThread(self)
        self.worker = GenerateWorker(scenes, self.speaker_combo.currentData(), output)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(lambda value, text: (self.progress.setValue(value), self.status.setText(text)))
        self.worker.done.connect(self.on_done)
        self.worker.failed.connect(self.on_failed)
        self.worker.done.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def on_done(self, path: str) -> None:
        self.generate_button.setEnabled(True)
        self.status.setText("اكتمل التوليد بصوت مصري")
        QMessageBox.information(self, "تم", f"تم حفظ الملف:\n{path}")

    def on_failed(self, message: str) -> None:
        self.generate_button.setEnabled(True)
        self.status.setText("حدث خطأ")
        QMessageBox.critical(self, "فشل التوليد", message)


def main() -> None:
    app = QApplication(sys.argv)
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
