# Руководство по автоматизации монтажа подкастов: MCP, Python и FFmpeg (macOS)

Настоящее руководство объединяет все материалы по использованию **Model Context Protocol (MCP)**, AI-ассистентов и программной обработке видеоряда подкастов (оверлеи, скругление углов, авто-центрирование, субтитры) с использованием **FFmpeg** и аппаратного ускорения **Apple Silicon (VideoToolbox)**.

---

## Оглавление
1. [Обзор экосистемы: MCP в производстве подкастов](#1-обзор-экосистемы-mcp-в-производстве-подкастов)
2. [Готовые скиллы и MCP-серверы для FFmpeg](#2-готовые-скиллы-и-mcp-серверы-для-ffmpeg)
3. [Работа со сторонними LLM и редакторами](#3-работа-со-сторонними-llm-и-редакторами)
4. [Автономный агент на LangGraph + OpenAI](#4-автономный-агент-на-langgraph--openai)
5. [Структура проекта и организация файлов](#5-структура-проекта-и-организация-файлов)
6. [Полный Python + FFmpeg пайплайн под macOS](#6-полный-python--ffmpeg-пайплайн-под-macos)
   * [Аппаратное ускорение (VideoToolbox)](#аппаратное-ускорение-videotoolbox)
   * [Авто-центрирование оверлея](#авто-центрирование-оверлея)
   * [Математическое скругление углов и Fade-анимация](#математическое-скругление-углов-и-fade-анимация)
   * [Динамический рендеринг субтитров (ASS Force Style)](#динамический-рендеринг-субтитров-ass-force-style)
7. [Сводный скрипт автоматизации](#7-сводный-скрипт-автоматизации)

---

## 1. Обзор экосистемы: MCP в производстве подкастов

**Model Context Protocol (MCP)** — это открытый стандарт для подключения внешних данных, утилит и API к LLM-ассистентам. В контексте работы с подкастами MCP-решения делятся на три класса:

### 1. Подкаст-платформы с MCP-интеграцией
* **Springcast (Podcast MCP):** Управление эпизодами через чат, автоматическая транскрипция, генерация таймкодов, шоу-ноутов и планирование публикаций.
* **Podigee (Podigee MCP Server):** Доступ ИИ к инфраструктуре подкаста, подтягивание исходников, генерация глав/тегов и аналитика прошлых выпусков.

### 2. Локальный пайплайн (DAW + MCP)
* **FFmpeg MCP Server:** Позволяет ИИ выполнять удаление тишины, EBU R128 (-16 LUFS) нормализацию громкости, нарезку Shorts/Reels по таймкодам и рендеринг оверлеев.
* **REAPER (Python / OSC / MCP):** Прямая автоматизация профессиональной DAW через `reapy` SDK (удаление пауз, расстановка маркеров, сведение по команде из чата).
* **Auphonic / Descript Wrappers:** Автоматический мастеринг и шумоподавление через REST API, спрятанное за MCP-оберткой.

---

## 2. Готовые скиллы и MCP-серверы для FFmpeg

Для работы с FFmpeg через Claude Desktop, Cursor или другие агенты доступны готовые решения:

### `ffmpeg-skill`
* **Особенности:** Набор из 40+ типизированных инструментов и MCP-сервер.
* **Возможности:** Авто-кадрирование (9:16), наложение оверлеев, работа со слоями, нормализация аудио.
* **Подключение в `claude_desktop_config.json`:**
  ```json
  {
    "mcpServers": {
      "ffmpeg-skill": {
        "command": "python3",
        "args": ["/Users/USERNAME/.claude/skills/ffmpeg-skill/mcp/server.py"]
      }
    }
  }
  ```

### `ffmpeg-usage` (Claude Desktop Skill)
* Набор инструкций (`SKILL.md`) с проверенными рецептами FFmpeg для быстрых манипуляций без разворачивания сторонних серверов.

### `ffmpeg-mcp-lite`
* Легковесный MCP-сервер.
* **Подключение:**
  ```json
  {
    "mcpServers": {
      "ffmpeg": {
        "command": "uvx",
        "args": ["ffmpeg-mcp-lite"]
      }
    }
  }
  ```

---

## 3. Работа со сторонними LLM и редакторами

* **Claude Code (CLI):** Официальный CLI-инструмент от Anthropic, жестко привязан к API Claude.
* **Использование сторонних LLM (GPT-4o, DeepSeek, Llama):**
  Поскольку MCP является открытым стандартом, его поддерживают сторонние среды:
  1. **Cursor / Windsurf:** Поддержка любых моделей + встроенный раздел `Settings > Features > MCP`.
  2. **VS Code + Cline / Roo Code:** Агентский режим в VS Code с возможностью подключать OpenRouter, Ollama, OpenAI и любые MCP-серверы.

---

## 4. Автономный агент на LangGraph + OpenAI

Связка **LangGraph + OpenAI + FFmpeg** позволяет создать цикличный пайплайн авто-монтажа с сохранением состояния (State):

```python
from typing import TypedDict, List
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI

class PodcastState(TypedDict):
    video_path: str
    transcript: str
    edit_plan: List[dict]
    status: str

llm = ChatOpenAI(model="gpt-4o", temperature=0)

def planning_node(state: PodcastState):
    # LLM анализирует транскрипт и генерирует JSON-план вставки графики
    mock_plan = [
        {"time": 10.0, "duration": 4.0, "text": "Рост аудитории Q3", "image": "chart.png"}
    ]
    return {"edit_plan": mock_plan, "status": "planned"}

def execution_node(state: PodcastState):
    # Вызов локальных FFmpeg-инструментов на Python
    return {"status": "completed"}

workflow = StateGraph(PodcastState)
workflow.add_node("planner", planning_node)
workflow.add_node("executor", execution_node)
workflow.set_entry_point("planner")
workflow.add_edge("planner", "executor")
workflow.add_edge("executor", END)

app = workflow.compile()
```

---

## 5. Структура проекта и организация файлов

При программном рендеринге все элементы подписей, масок и центрирования рассчитываются **«на лету»** в памяти. Промежуточные графические файлы или видеосегменты на диск не сохраняются.

### Типовая директория проекта:
```text
my_podcast_project/
├── podcast_sample.mp4       # Исходное видео подкаста
├── screenshot.png           # Исходная картинка для оверлея
├── subtitles.srt            # Файл субтитров (необязательно, если используются оверлеи)
├── render_podcast.py        # Исполняемый Python-скрипт
└── podcast_output.mp4       # Готовый отрендеренный файл
```

---

## 6. Полный Python + FFmpeg пайплайн под macOS

### Аппаратное ускорение (VideoToolbox)
На macOS (чипы Apple Silicon M1/M2/M3/M4) используется аппаратный кодер `-c:v h264_videotoolbox`. Управление качеством происходит через параметр `-q:v` (оптимально `65-70`), вместо стандартного `-crf`.

### Авто-центрирование оверлея
Ширина и высота видео определяются через `ffprobe`. Формула центрирования:
$$X_{center} = \frac{W_{video} - W_{overlay}}{2}$$
$$Y_{center} = \frac{H_{video} - H_{overlay}}{2}$$

Текст подписи центрируется относительно самой картинки через выражение FFmpeg:
$$X_{text} = \frac{W_{overlay} - W_{text}}{2}$$

### Математическое скругление углов и Fade-анимация
Скругление углов реализовано через фильтр `geq` (радиус $R=20\text{px}$):
```text
geq=lum='p(X,Y)':a='if(lt(hypot(X-20,Y-20),20)*lt(X,20)*lt(Y,20),if(lte(hypot(X-20,Y-20),20),255,0),...)'
```
Плавное появление и исчезновение альфа-канала управляется фильтром `fade=t=in...:alpha=1`.

---

## 7. Сводный скрипт автоматизации

Ниже представлен единый рабочий скрипт для macOS, объединяющий авто-центрирование, аппаратное ускорение, скругление углов, создание подписи на подложке и рендеринг субтитров.

```python
import subprocess
import os
import json

def get_video_dimensions(video_path: str):
    """Определяет ширину и высоту видео через ffprobe."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "json", video_path
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Ошибка ffprobe: {result.stderr}")
    data = json.loads(result.stdout)
    return data["streams"][0]["width"], data["streams"][0]["height"]

def get_macos_font():
    """Возвращает доступный системный шрифт macOS."""
    font_paths = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/SFNS.ttf"
    ]
    for path in font_paths:
        if os.path.exists(path):
            return path
    return "Arial"

def render_podcast_overlay(
    input_video: str, 
    overlay_img: str, 
    output_video: str, 
    caption_text: str,
    start_time: float = 3.0, 
    duration: float = 5.0,
    overlay_width: int = 400
):
    """
    Накладывает стильный оверлей с подписью на видео (Apple Silicon VideoToolbox).
    """
    vid_w, vid_h = get_video_dimensions(input_video)
    x_center = (vid_w - overlay_width) // 2
    y_center = (vid_h - overlay_width) // 2

    end_time = start_time + duration
    fade_dur = 0.3
    font_path = get_macos_font()

    filter_complex = (
        f"[1:v]scale={overlay_width}:-1[scaled_img];"
        
        # Скругление углов (R=20px)
        "[scaled_img]format=yuva420p,"
        "geq=lum='p(X,Y)':a='if(lt(hypot(X-20,Y-20),20)*lt(X,20)*lt(Y,20),"
        "if(lte(hypot(X-20,Y-20),20),255,0),"
        "if(lt(hypot(X-(w-20),Y-20),20)*gt(X,w-20)*lt(Y,20),"
        "if(lte(hypot(X-(w-20),Y-20),20),255,0),"
        "if(lt(hypot(X-20,Y-(h-20)),20)*lt(X,20)*gt(Y,h-20),"
        "if(lte(hypot(X-20,Y-(h-20)),20),255,0),"
        "if(lt(hypot(X-(w-20),Y-(h-20)),20)*gt(X,w-20)*gt(Y,h-20),"
        "if(lte(hypot(X-(w-20),Y-(h-20)),20),255,0),"
        "255))))'[rounded];"

        # Подпись с полупрозрачной подложкой
        f"[rounded]drawtext=fontfile='{font_path}':text='{caption_text}':"
        "fontcolor=white:fontsize=22:box=1:boxcolor=black@0.6:boxborderw=10:"
        "x=(w-text_w)/2:y=h-th-15[with_text];"

        # Fade in / Fade out
        f"[with_text]fade=t=in:st={start_time}:d={fade_dur}:alpha=1,"
        f"fade=t=out:st={end_time - fade_dur}:d={fade_dur}:alpha=1[final_overlay];"

        # Наложение по центру
        f"[0:v][final_overlay]overlay=x={x_center}:y={y_center}:enable='between(t,{start_time},{end_time})'[outv]"
    )

    cmd = [
        "ffmpeg",
        "-i", input_video,
        "-i", overlay_img,
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-map", "0:a?", 
        "-c:v", "h264_videotoolbox",
        "-q:v", "65",
        "-c:a", "copy",
        "-y",
        output_video
    ]

    print("Рендеринг оверлея с помощью VideoToolbox...")
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        print("Ошибка:", res.stderr)
    else:
        print(f"Файл успешно создан: {output_video}")

def render_stylish_subtitles(
    input_video: str,
    srt_path: str,
    output_video: str,
    style_preset: str = "modern_box"
):
    """
    Вшивание стилизованных субтитров через ASS force_style.
    """
    font_path = get_macos_font()
    
    if style_preset == "modern_box":
        style = (
            f"FontName={font_path},FontSize=24,PrimaryColour=&H00FFFFFF,"
            "BackColour=&H80000000,BorderStyle=3,Outline=4,Shadow=0,Alignment=2,MarginV=50"
        )
    else:  # youtube_pop
        style = (
            f"FontName={font_path},FontSize=28,PrimaryColour=&H0000FFFF,"
            "OutlineColour=&H00000000,BackColour=&H40000000,BorderStyle=1,Outline=3,Shadow=2,Alignment=2,MarginV=60"
        )

    escaped_srt = srt_path.replace(":", "\\:").replace("'", "\\'")
    filter_complex = f"subtitles='{escaped_srt}':force_style='{style}'"

    cmd = [
        "ffmpeg", "-i", input_video,
        "-vf", filter_complex,
        "-c:v", "h264_videotoolbox", "-q:v", "65",
        "-c:a", "copy", "-y", output_video
    ]

    print(f"Рендеринг субтитров [{style_preset}]...")
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        print("Ошибка:", res.stderr)
    else:
        print(f"Субтитры применены: {output_video}")

if __name__ == "__main__":
    # Пример вызова функций
    render_podcast_overlay(
        input_video="podcast_sample.mp4",
        overlay_img="screenshot.png",
        output_video="podcast_with_overlay.mp4",
        caption_text="Статистика за Q3",
        start_time=3.0,
        duration=4.0
    )
```