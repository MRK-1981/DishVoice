from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Qt
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGridLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QProgressBar,
    QWidget,
)


TIME_RE = re.compile(
    r"^\s*(?P<start>\d{1,2}:\d{2}(?::\d{2})?)\s*[-–>]\s*"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?)\s*[|]\s*(?P<text>.+?)\s*$"
)


def bundled_tool(name: str) -> str:
    """Use bundled Windows tools in the EXE folder, or PATH during development."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    candidate = base / "bin" / (name + (".exe" if sys.platform == "win32" else ""))
    return str(candidate) if candidate.exists() else name


@dataclass
class Scene:
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return self.end - self.start


def parse_time(value: str) -> float:
    parts = [int(x) for x in value.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds
    hours, minutes, seconds = parts
    return hours * 3600 + minutes * 60 + seconds


def parse_scenes(raw: str) -> list[Scene]:
    scenes: list[Scene] = []
    for line_no, line in enumerate(raw.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = TIME_RE.match(line)
        if not match:
            raise ValueError(
                f"السطر {line_no} غير مفهوم. استخدم: 00:00 -> 00:07 | النص"
            )
        start = parse_time(match.group("start"))
        end = parse_time(match.group("end"))
        if end <= start:
            raise ValueError(f"السطر {line_no}: وقت النهاية لازم يكون بعد البداية")
        scenes.append(Scene(start, end, match.group("text")))

    if not scenes:
        raise ValueError("أدخل مشهدًا واحدًا على الأقل")

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
    # ffmpeg يقبل atempo بين 0.5 و2.0؛ نقسم النسبة عند الحاجة.
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
    # output = input / atempo، لذلك النسبة هي actual / target.
    tempo = actual / seconds
    audio_filter = (
        f"{atempo_filter(tempo)},apad=pad_dur={seconds:.6f},"
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

    def __init__(self, scenes: list[Scene], model_path: Path, output_path: Path):
        super().__init__()
        self.scenes = scenes
        self.model_path = model_path
        self.output_path = output_path

    def run(self) -> None:
        try:
            from piper import PiperVoice

            config_path = Path(str(self.model_path) + ".json")
            if not config_path.exists():
                raise FileNotFoundError(
                    f"ملف إعداد الصوت غير موجود بجانب النموذج: {config_path.name}"
                )
            voice = PiperVoice.load(str(self.model_path), config_path=str(config_path))

            with tempfile.TemporaryDirectory(prefix="dishvoice_") as temp_dir:
                temp = Path(temp_dir)
                timed_files: list[Path] = []
                for index, scene in enumerate(self.scenes, 1):
                    self.progress.emit(
                        int((index - 1) / len(self.scenes) * 90),
                        f"توليد المشهد {index} من {len(self.scenes)}...",
                    )
                    raw_wav = temp / f"raw_{index:03d}.wav"
                    timed_wav = temp / f"timed_{index:03d}.wav"
                    with wave.open(str(raw_wav), "wb") as wav_file:
                        voice.synthesize_wav(scene.text, wav_file)
                    retime_audio(raw_wav, timed_wav, scene.duration)
                    timed_files.append(timed_wav)

                concat_file = temp / "concat.txt"
                concat_file.write_text(
                    "".join(f"file '{p.as_posix()}'\n" for p in timed_files),
                    encoding="utf-8",
                )
                self.progress.emit(92, "دمج المشاهد وضبط الخط الزمني...")
                self.output_path.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    [bundled_tool("ffmpeg"), "-y", "-f", "concat", "-safe", "0", "-i",
                     str(concat_file), "-c:a", "pcm_s16le", str(self.output_path)],
                    capture_output=True, text=True, check=True,
                )
            self.progress.emit(100, "اكتمل التوليد")
            self.done.emit(str(self.output_path))
        except Exception as exc:  # عرض الخطأ للمستخدم بدل إغلاق التطبيق.
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("DishVoice — فويس أوفر مصري محلي")
        self.resize(1000, 720)
        self.thread: QThread | None = None
        self.worker: GenerateWorker | None = None

        central = QWidget()
        layout = QGridLayout(central)
        layout.setColumnStretch(1, 1)

        layout.addWidget(QLabel("موديل Piper (.onnx):"), 0, 0)
        self.model_edit = QLineEdit()
        layout.addWidget(self.model_edit, 0, 1)
        model_button = QPushButton("اختيار")
        model_button.clicked.connect(self.choose_model)
        layout.addWidget(model_button, 0, 2)

        layout.addWidget(QLabel("ملف الإخراج (.wav):"), 1, 0)
        self.output_edit = QLineEdit(str(Path.home() / "Desktop" / "voiceover.wav"))
        layout.addWidget(self.output_edit, 1, 1)
        output_button = QPushButton("اختيار")
        output_button.clicked.connect(self.choose_output)
        layout.addWidget(output_button, 1, 2)

        layout.addWidget(QLabel("المشاهد — كل سطر: البداية -> النهاية | النص"), 2, 0, 1, 3)
        self.script_edit = QPlainTextEdit()
        self.script_edit.setPlaceholderText(
            "00:00 -> 00:07 | عمرك سألت نفسك...\n"
            "00:07 -> 00:17 | الغسالات الحديثة..."
        )
        layout.addWidget(self.script_edit, 3, 0, 1, 3)

        self.progress = QProgressBar()
        layout.addWidget(self.progress, 4, 0, 1, 2)
        self.status = QLabel("جاهز")
        layout.addWidget(self.status, 4, 2)

        self.generate_button = QPushButton("توليد الفويس أوفر")
        self.generate_button.clicked.connect(self.generate)
        layout.addWidget(self.generate_button, 5, 0, 1, 3)

        self.setCentralWidget(central)

    def choose_model(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "اختيار نموذج Piper", "", "Piper model (*.onnx)"
        )
        if path:
            self.model_edit.setText(path)

    def choose_output(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "حفظ الفويس أوفر", self.output_edit.text(), "WAV audio (*.wav)"
        )
        if path:
            self.output_edit.setText(path)

    def generate(self) -> None:
        try:
            scenes = parse_scenes(self.script_edit.toPlainText())
            model = Path(self.model_edit.text().strip())
            output = Path(self.output_edit.text().strip())
            if model.suffix.lower() != ".onnx" or not model.exists():
                raise ValueError("اختر ملف نموذج Piper بصيغة .onnx موجودًا على الجهاز")
            if output.suffix.lower() != ".wav":
                raise ValueError("ملف الإخراج لازم يكون بصيغة .wav في النسخة الأولى")
        except Exception as exc:
            QMessageBox.warning(self, "راجع البيانات", str(exc))
            return

        self.generate_button.setEnabled(False)
        self.progress.setValue(0)
        self.status.setText("بدء التوليد...")
        self.thread = QThread(self)
        self.worker = GenerateWorker(scenes, model, output)
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
        self.status.setText("اكتمل التوليد")
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
