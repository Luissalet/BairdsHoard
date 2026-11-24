"""The copilot without the app: what an assistant may send, finding text on a text map, and the runner driven tick by
tick with made-up frames, a made-up clock and a made-up text reader."""

from __future__ import annotations

import contextlib
import threading
import time

import pytest

from baird_hoard.config import DEFAULT_COPILOT
from baird_hoard.copilot import TAP_H, TAP_W, Copilot, changed
from baird_hoard.copilot_steps import Space, clean_keys, clean_step
from baird_hoard.errors import BairdError
from baird_hoard.textmap import count_text, find_text, scaled

W, H = 1920, 1080


def line(text: str, x: int, y: int, h: int = 14) -> list:
    """A line of a text map: every word about 8 px per letter wide."""
    words, cx = [], x
    for word in text.split():
        words.append([cx, y, 8 * len(word), h, word])
        cx += 8 * len(word) + 6
    return [x, y, cx - 6 - x, h, text, words]


SCREEN = [line("Archivo Editar Ver", 10, 40), line("Sincronizar cambios", 1400, 80), line("Publicar rama", 1700, 80),
          line("3 cambios pendientes", 20, 400), line("Confirmar", 20, 440), line("Guardar(G)", 900, 700), line("Ir", 300, 40)]


# ---------------------------------------------------------------------------------------------- text on screen

def test_find_text_prefers_the_exact_label():
    found = find_text(SCREEN, "publicar rama")
    assert found[0]["exact"] and found[0]["text"] == "Publicar rama"
    assert found[0]["box"][0] == 1700


def test_find_text_reads_through_accents_case_and_adornments():
    assert find_text([line("CONTROL DE CÓDIGO FUENTE", 0, 0)], "control de codigo fuente")[0]["exact"]
    part = find_text(SCREEN, "Guardar")
    assert part and not part[0]["exact"] and part[0]["text"] == "Guardar(G)"


def test_find_text_never_lands_on_an_unrelated_word():
    assert find_text(SCREEN, "ir")[0]["text"] == "Ir"
    without = [l for l in SCREEN if l[4] != "Ir"]
    assert find_text(without, "ir") == []                 # "Confirmar" contains it, and is not it
    assert find_text(SCREEN, "Descargar") == []


def test_find_text_tolerates_what_ocr_gets_wrong():
    lines = [line("no changes added to cownit", 0, 0), line("READMEmd", 0, 30)]
    assert find_text(lines, "added to commit")[0]["text"] == "added to cownit"
    assert find_text(lines, "commit") == []                # one short word, two letters off: too far to trust
    assert find_text(lines, "README.md")[0]["exact"]


def test_find_text_lists_every_place_in_reading_order():
    found = find_text(SCREEN, "cambios")
    assert [m["box"][1] for m in found] == [80, 400]
    assert count_text(SCREEN, "cambios") == 2


def test_scaled_text_map_goes_back_to_picture_pixels():
    big = {"w": 3840, "h": 2160, "lines": [[20, 80, 200, 28, "Hola", [[20, 80, 200, 28, "Hola"]]]]}
    small = scaled(big, 2)
    assert (small["w"], small["h"]) == (1920, 1080)
    assert small["lines"][0][:5] == [10, 40, 100, 14, "Hola"] and small["lines"][0][5][0] == [10, 40, 100, 14, "Hola"]


# ---------------------------------------------------------------------------------------------- what an assistant sends

def test_space_maps_a_look_and_a_zoomed_look():
    half = Space((960, 540))
    assert half.point(480, 270) == (0.5, 0.5)
    assert half.px(0.25, 0.5) == [240, 270]
    zoom = Space((800, 450), (0.5, 0.5, 0.25, 0.25))       # the assistant looked at a quarter-wide part of the picture
    assert zoom.point(400, 225) == (0.625, 0.625)
    assert zoom.px(0.625, 0.625) == [400, 225]
    with pytest.raises(BairdError):
        Space(None).point(1, 1)


def test_step_shorthands_become_marks():
    space = Space((W, H))
    step = clean_step({"say": "Abre el menú", "click": "Archivo"}, space)
    assert step["_mode"] == "change" and step["marks"][0]["find"] == "Archivo" and step["marks"][0]["state"] == "searching"
    step = clean_step({"click": [960, 540]}, space)
    assert step["say"] == "Pulsa aquí" and (step["marks"][0]["x"], step["marks"][0]["y"]) == (0.5, 0.5)
    step = clean_step({"say": "Nombre", "type": "informe-final", "at": {"find": "Nombre de archivo", "side": "right", "dx": 192}}, space)
    mark = step["marks"][0]
    assert step["_mode"] == "typed" and step["_probe"] == "informe-final" and mark["kind"] == "type"
    assert mark["_anchor"]["side"] == "right" and mark["_anchor"]["dx"] == pytest.approx(0.1)
    step = clean_step({"say": "Arrastra", "arrow": [[192, 108], "Papelera"]}, space)
    assert step["marks"][0]["x"] == 0.1 and step["marks"][0]["_to_anchor"]["text"] == "Papelera"
    step = clean_step({"say": "Guarda", "keys": "ctrl+s"}, space)
    assert step["keys"] == [["Ctrl", "S"]] and step["_mode"] == "change" and step["marks"] == []
    step = clean_step({"say": "Mira este panel", "box": [0, 0, 960, 540]}, space)
    assert step["marks"][0]["box"] == [0.0, 0.0, 0.5, 0.5] and step["_mode"] == "manual"      # looking is not an action
    step = clean_step("Respira", space)
    assert step["_mode"] == "manual" and step["marks"] == []
    step = clean_step({"say": "Varias", "marks": [{"click": "Aceptar"}, {"note": "No toques esto", "at": [100, 100]}, "Cancelar"]}, space)
    assert [m["kind"] for m in step["marks"]] == ["click", "note", "click"] and [m["id"] for m in step["marks"]] == ["m0", "m1", "m2"]
    assert clean_step({"say": "Espera", "until": {"text": "Completado"}}, space)["_mode"] == "text"
    assert clean_step({"say": "Espera", "until": 3}, space)["until"] == {"mode": "after", "seconds": 3.0}
    assert clean_step({"say": "Pulsa", "at": "Guardar"}, space)["marks"][0]["kind"] == "click"


def test_keys_are_read_as_key_caps():
    assert clean_keys("Ctrl+K, Ctrl+S") == [["Ctrl", "K"], ["Ctrl", "S"]]
    assert clean_keys(["alt+tab", "enter"]) == [["Alt", "Tab"], ["Intro"]]
    assert clean_keys("ctrl++") == [["Ctrl", "+"]]


@pytest.mark.parametrize("bad", [{}, {"say": "x", "click": [1, 2, 3]}, {"say": "x", "colour": "red"}, {"type": "", "at": [1, 1]},
                                 {"say": "x", "box": [10, 10]}, {"say": "x", "arrow": [10, 10]}, {"say": "x", "until": "pronto"},
                                 {"say": "x", "click": {"find": "a", "side": "diagonal"}}, 7])
def test_mistakes_are_refused_with_a_message(bad):
    with pytest.raises(BairdError) as err:
        clean_step(bad, Space((W, H)))
    assert err.value.code == "invalid"


def test_coordinates_without_a_known_size_ask_for_a_look():
    with pytest.raises(BairdError) as err:
        clean_step({"say": "x", "click": [10, 10]}, Space(None))
    assert "Look first" in err.value.hint
    assert clean_step({"say": "x", "click": "Guardar"}, Space(None))["marks"][0]["find"] == "Guardar"     # a text needs no size


def test_changed_counts_cells_and_boxes():
    blank = bytes(TAP_W * TAP_H)
    assert changed(blank, blank) == 0
    assert changed(blank, paint((0.5, 0.5, 0.25, 0.5))) == pytest.approx(0.125, abs=0.01)
    assert changed(blank, paint((0.5, 0.5, 0.25, 0.5)), [0.5, 0.5, 0.25, 0.5]) == 1.0
    assert changed(blank, paint((0.5, 0.5, 0.25, 0.5)), [0.0, 0.0, 0.25, 0.25]) == 0.0


# ---------------------------------------------------------------------------------------------- the runner

def paint(*boxes, base: bytes | None = None) -> bytes:
    """A grey frame with the given rectangles (fractions) lit."""
    data = bytearray(base or bytes(TAP_W * TAP_H))
    for x, y, w, h in boxes:
        for row in range(int(y * TAP_H), min(TAP_H, int((y + h) * TAP_H))):
            for col in range(int(x * TAP_W), min(TAP_W, int((x + w) * TAP_W))):
                data[row * TAP_W + col] = 200
    return bytes(data)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Frames:
    def __init__(self, clock: Clock):
        self.clock, self.seq, self.data = clock, 0, bytes(TAP_W * TAP_H)

    def show(self, data: bytes) -> None:
        self.data = data

    def latest(self):
        self.seq += 1                      # a new frame every time the runner asks
        return self.seq, self.data, self.clock()


class Engine:
    demands = 0

    @contextlib.contextmanager
    def request(self):
        yield

    def wait_for_picture(self) -> None:
        pass

    def snapshot(self) -> dict:
        return {"state": "running", "input": {"width": W, "height": H}}

    def set_demand(self, key, active: bool) -> None:
        self.demands += 1 if active else -1


class Library:
    def input_size(self):
        return W, H

    def grab_file(self, region, dest, *, scale: float = 1.0):
        dest.write_bytes(b"BM")
        self.scale = scale
        return dest

    def grab_jpeg(self, region, max_side=1024, quality=4):
        return b"\xff\xd8fake", max_side, round(max_side * H / W)


class Reader:
    """Stands in for the Windows OCR: answers with whatever ``lines`` holds, at the size the picture was enlarged to."""

    def __init__(self, library: Library):
        self.library, self.lines, self.reads, self.works = library, list(SCREEN), 0, True

    def available(self) -> bool:
        return True

    def read(self, path, lang=""):
        self.reads += 1
        if not self.works:
            raise BairdError("unavailable", "no OCR here")
        k = self.library.scale
        big = [[l[0] * k, l[1] * k, l[2] * k, l[3] * k, l[4], [[w[0] * k, w[1] * k, w[2] * k, w[3] * k, w[4]] for w in l[5]]] for l in self.lines]
        return {"w": W * k, "h": H * k, "lines": big}

    def stop(self) -> None:
        pass


class Settings(dict):
    pass


@pytest.fixture
def rig(tmp_path):
    clock = Clock()
    library = Library()
    frames, reader, engine = Frames(clock), Reader(library), Engine()
    settings = Settings(copilot=dict(DEFAULT_COPILOT))
    cp = Copilot(engine=engine, library=library, settings=settings, reader=reader, ffmpeg=None, work_dir=tmp_path, frames=frames,
                 clock=clock, background=False)
    cp.t = clock            # type: ignore[attr-defined]
    cp.f = frames           # type: ignore[attr-defined]
    cp.r = reader           # type: ignore[attr-defined]
    cp.e = engine           # type: ignore[attr-defined]
    yield cp
    cp.close()


def run(cp: Copilot, seconds: float, step: float = 0.2) -> None:
    for _ in range(round(seconds / step)):
        cp.t.now += step
        cp._tick()


def events(cp: Copilot) -> list[str]:
    return [e["event"] for e in cp.wait(0)["events"]]


def test_a_click_step_ends_when_the_screen_changes_around_the_mark(rig):
    out = rig.show({"agent": "Ana", "goal": "Publicar", "steps": [{"say": "Pulsa aquí", "click": [960, 540]}, {"say": "Y aquí", "click": [200, 200]}]})
    assert out == {"session": "active", "step": 1, "of": 2, "say": "Pulsa aquí", "ends_on": "change", "space": [W, H]}
    assert rig.e.demands == 1
    run(rig, 1.5)                                          # the screen is still: the step starts watching
    assert rig.state()["session"]["step"]["auto"] is True
    rig.f.show(paint((0.45, 0.42, 0.12, 0.2)))             # a menu opens where the mark is
    run(rig, 1.2)
    assert rig.status()["step"] == 2 and events(rig) == ["advanced"]
    rig.f.show(paint((0.05, 0.1, 0.12, 0.2)))
    run(rig, 1.2)
    out = rig.wait(0)
    assert out["session"] == "done" and [e["event"] for e in out["events"]] == ["advanced", "done"]
    assert out["look"] and out["_image"]["mime"] == "image/jpeg" and "finished" in out["next"]


def test_the_next_step_watches_from_the_moment_the_last_one_ended(rig):
    rig.show({"steps": [{"say": "Uno", "click": [960, 540]}, {"say": "Dos", "click": [200, 200]}]})
    run(rig, 1.5)
    rig.f.show(paint((0.45, 0.42, 0.12, 0.2)))
    run(rig, 1.0)
    assert rig.status()["step"] == 2
    rig.f.show(paint((0.45, 0.42, 0.12, 0.2), (0.05, 0.1, 0.12, 0.2)))      # the person is quick: no pause before the second click
    run(rig, 1.0)
    assert rig.status()["session"] == "done"


def test_a_notification_in_a_corner_does_not_end_the_step_but_a_dialog_elsewhere_does(rig):
    rig.show({"steps": [{"say": "Pulsa aquí", "click": [300, 300]}, {"say": "Y aquí", "click": [300, 300]}]})
    run(rig, 1.5)
    rig.f.show(paint((0.76, 0.8, 0.22, 0.14)))             # 3 % of the screen, in the bottom right corner
    run(rig, 3)
    assert rig.status()["step"] == 1 and rig.status()["session"] == "active"
    rig.f.show(paint((0.76, 0.03, 0.22, 0.14)))            # or in the top right one
    run(rig, 3)
    assert rig.status()["step"] == 1
    rig.f.show(paint((0.76, 0.03, 0.22, 0.14), (0.45, 0.4, 0.25, 0.14)))     # a dialog opens in the middle, far from the mark
    run(rig, 1.2)
    assert rig.status()["step"] == 2
    rig.f.show(paint((0.76, 0.03, 0.22, 0.14), (0.45, 0.4, 0.25, 0.14), (0.9, 0.5, 0.05, 0.05)))     # too little, too far
    run(rig, 3)
    assert rig.status()["step"] == 2
    rig.f.show(paint((0.0, 0.0, 1.0, 0.5)))                # the whole page went somewhere else
    run(rig, 1.2)
    assert rig.status()["session"] == "done"


def test_a_mark_in_the_corner_still_sees_what_happens_there(rig):
    rig.show({"steps": [{"say": "Cierra el aviso", "click": [1800, 1000]}]})
    run(rig, 1.5)
    rig.f.show(paint((0.88, 0.86, 0.1, 0.1)))
    run(rig, 1.2)
    assert rig.status()["session"] == "done"


def test_garbled_text_from_a_windows_console_is_put_right(rig):
    from baird_hoard.copilot_steps import repair
    garbled = "Pulsa «Selección», aquí".encode("utf-8").decode("cp1252")
    assert garbled != "Pulsa «Selección», aquí" and repair(garbled) == "Pulsa «Selección», aquí"
    assert repair("ÁRBOL".encode("utf-8").decode("latin-1")) == "ÁRBOL"                  # a byte the code page leaves undefined
    for fine in ("Pulsa «Selección», aquí", "São Paulo", "Â", "Ã", "100 € — fin", ""):
        assert repair(fine) == fine
    assert repair({"say": garbled, "marks": [{"click": "Selecci\u00c3\u00b3n"}], "n": 3}) == {"say": "Pulsa «Selección», aquí", "marks": [{"click": "Selección"}], "n": 3}
    rig.r.lines = [line("Selección", 127, 45)]
    rig.show({"steps": [{"say": garbled, "click": "Selecci\u00c3\u00b3n"}]})
    state = rig.state()["session"]["step"]
    assert state["say"] == "Pulsa «Selección», aquí" and state["marks"][0]["state"] == "ok" and state["marks"][0]["find"] == "Selección"
    rig.stop("Â¡Listo!")
    assert rig.state()["session"]["message"] == "¡Listo!"


def test_something_that_flashes_and_goes_away_is_not_a_step_done(rig):
    rig.show({"steps": [{"say": "Pulsa aquí", "click": [960, 540]}]})
    run(rig, 1.5)
    rig.f.show(paint((0.45, 0.42, 0.12, 0.2)))
    run(rig, 0.2)
    rig.f.show(bytes(TAP_W * TAP_H))                       # back to what it was before the screen settled
    run(rig, 2)
    assert rig.status()["session"] == "active"


def test_a_screen_that_never_stops_moving_leaves_the_step_to_the_person(rig):
    rig.show({"steps": [{"say": "Pulsa aquí", "click": [960, 540]}]})
    for n in range(40):                                    # a video is playing
        rig.f.show(paint((0.1, 0.1, 0.5, 0.5)) if n % 2 else paint((0.3, 0.3, 0.5, 0.5)))
        run(rig, 0.2)
    state = rig.state()["session"]
    assert state["status"] == "active" and state["step"]["auto"] is False and rig.status()["ends_on"] == "manual"
    rig.user("next")
    assert rig.status()["session"] == "done"


def test_text_marks_are_placed_followed_and_reported(rig):
    out = rig.show({"steps": [{"say": "Publica", "click": "Publicar rama"}, {"say": "Acepta", "click": "Aceptar"}]})
    assert "searching" not in out
    mark = rig.state()["session"]["step"]["marks"][0]
    assert mark["state"] == "ok" and mark["x"] == pytest.approx((1700 + 53) / W, abs=0.01) and mark["y"] == pytest.approx(87 / H, abs=0.01)
    run(rig, 1.5)
    # the page scrolls under the menu bar: the label goes down with its neighbours, and that is not the step being done
    rig.r.lines = [l if l[1] == 40 else line(l[4], l[0], l[1] + 300) for l in SCREEN]
    rig.f.show(paint((0.0, 0.0, 1.0, 0.6)))
    run(rig, 1.5)
    assert rig.status()["step"] == 1
    assert rig.state()["session"]["step"]["marks"][0]["y"] == pytest.approx(387 / H, abs=0.01)
    # now the dialog opens over it: the label is gone and the step is done
    rig.r.lines = [line("Nombre de la rama remota", 700, 420)]
    rig.f.show(paint((0.3, 0.3, 0.4, 0.3)))
    run(rig, 1.5)
    now = rig.status()
    assert now["step"] == 2 and now["searching"] == ["Aceptar"]
    assert rig.state()["session"]["step"]["marks"][0]["state"] == "searching"
    run(rig, 7)                                            # still not there: the assistant is told once
    out = rig.wait(0)
    missing = [e for e in out["events"] if e["event"] == "anchor_missing"]
    assert len(missing) == 1 and missing[0]["texts"] == ["Aceptar"] and missing[0]["step"] == 2
    assert "not on screen" in out["next"]
    # the button shows up: the mark appears, and its arrival is not taken for the click
    rig.r.lines = [line("Nombre de la rama remota", 700, 420), line("Aceptar", 1100, 640)]
    rig.f.show(paint((0.3, 0.3, 0.4, 0.3), (0.55, 0.57, 0.1, 0.05)))
    run(rig, 2.5)
    assert rig.status()["step"] == 2 and rig.state()["session"]["step"]["marks"][0]["state"] == "ok"
    rig.f.show(bytes(TAP_W * TAP_H))
    rig.r.lines = []
    run(rig, 1.5)
    assert rig.status()["session"] == "done"


def test_the_reading_that_ends_a_step_places_the_next_marks_at_once(rig):
    rig.show({"steps": [{"say": "Publica", "click": "Publicar rama"}, {"say": "Confirma", "click": "Confirmar"}]})
    run(rig, 1.5)
    reads = rig.r.reads
    rig.f.show(paint((0.3, 0.3, 0.4, 0.3)))
    run(rig, 1.5)
    now = rig.state()["session"]
    assert now["index"] == 1 and now["step"]["marks"][0]["state"] == "ok"
    assert rig.r.reads == reads + 1                         # the reading that confirmed the change, and no other


def test_the_same_words_turning_up_elsewhere_are_the_step_done_not_a_scroll(rig):
    # Clicking "Confirmar" in a side panel opens a page titled "Confirmar": the title comes first in reading order.
    rig.show({"steps": [{"say": "Confirma", "click": "Confirmar"}, {"say": "Sigue", "click": [200, 200]}]})
    run(rig, 1.5)
    rig.r.lines = [line("Confirmar", 30, 8), line("Buscar en Confirmar", 1500, 40)] + SCREEN
    rig.f.show(paint((0.2, 0.1, 0.7, 0.6)))
    run(rig, 1.5)
    assert rig.status()["step"] == 2 and events(rig) == ["advanced"]


def test_a_text_that_leaves_its_place_alone_is_the_step_done(rig):
    # The menu closes and its entry is now a heading somewhere else, while every other line stays put.
    rig.show({"steps": [{"say": "Confirma", "click": "Confirmar"}, {"say": "Sigue", "click": [200, 200]}]})
    run(rig, 1.5)
    rig.r.lines = [l for l in SCREEN if l[4] != "Confirmar"] + [line("Confirmar", 800, 160)]
    rig.f.show(paint((0.3, 0.1, 0.5, 0.5)))
    run(rig, 1.5)
    assert rig.status()["step"] == 2


def test_a_placed_mark_keeps_to_its_own_copy_of_the_text(rig):
    rig.show({"steps": [{"say": "Confirma", "click": "Confirmar"}]})
    run(rig, 1.5)
    where = rig.state()["session"]["step"]["marks"][0]
    assert where["y"] == pytest.approx(447 / H, abs=0.01)
    rig.r.lines = [line("Confirmar", 30, 8)] + SCREEN       # the word also shows in a corner notice: not our button
    rig.f.show(paint((0.9, 0.0, 0.1, 0.1)))
    run(rig, 3.0)
    mark = rig.state()["session"]["step"]["marks"][0]
    assert rig.status()["step"] == 1 and mark["state"] == "ok"
    assert (mark["x"], mark["y"]) == (where["x"], where["y"])
    assert "several_on_screen" not in rig.status()          # what was said when it was placed still holds


def test_carried_tells_a_scroll_from_the_same_words_elsewhere():
    from baird_hoard.copilot import carried, line_places

    def places(lines):
        return line_places({"w": W, "h": H, "lines": lines})

    before = places(SCREEN)
    down = places([l if l[1] == 40 else line(l[4], l[0], l[1] + 300) for l in SCREEN])
    assert carried(before, down, "Publicar rama", 0.0, 300 / H)             # its neighbours went down with it
    assert not carried(before, before, "Publicar rama", 0.0, 300 / H)       # nobody else moved
    assert not carried(before, down, "Publicar rama", 0.3, 0.1)             # they moved, but not that way
    assert carried({}, {}, "Publicar rama", 0.0, 0.2) and not carried({}, {}, "Publicar rama", 0.3, 0.2)
    one = places([line("Confirmar", 20, 740) if l[4] == "Confirmar" else l for l in SCREEN])
    assert carried(before, one, "Publicar rama", 0.0, 300 / H)             # a long scroll: one line left to vouch for it
    aside = places([line("Confirmar", 320, 740) if l[4] == "Confirmar" else l for l in SCREEN])
    assert not carried(before, aside, "Publicar rama", 300 / W, 300 / H)    # a slanted move needs more than one
    twice = places(SCREEN + [line("Confirmar", 900, 900)])
    assert twice["confirmar"] is None and before["confirmar"] is not None  # a text that is there twice says nothing


def test_the_same_text_twice_is_said_and_can_be_picked(rig):
    out = rig.show({"steps": [{"say": "x", "click": "cambios"}]})
    assert out["several_on_screen"] == {"cambios": 2}
    assert rig.state()["session"]["step"]["marks"][0]["y"] == pytest.approx(87 / H, abs=0.01)
    out = rig.show({"steps": [{"say": "x", "click": {"find": "cambios", "nth": 2}}]})
    assert "several_on_screen" not in out
    assert rig.state()["session"]["step"]["marks"][0]["y"] == pytest.approx(407 / H, abs=0.01)
    rig.show({"steps": [{"say": "x", "click": {"find": "cambios", "near": [100, 420]}}]})
    assert rig.state()["session"]["step"]["marks"][0]["y"] == pytest.approx(407 / H, abs=0.01)
    rig.show({"steps": [{"say": "x", "type": "hola", "at": {"find": "Confirmar", "side": "right"}}]})
    mark = rig.state()["session"]["step"]["marks"][0]
    assert mark["x"] > (20 + 72) / W and mark["box"] is None


def test_a_typing_step_ends_when_the_text_shows_up(rig):
    rig.r.lines = [line("PS C: git status", 320, 800)]
    rig.show({"steps": [{"say": "Escribe", "type": "git status", "at": [400, 900]}]})
    assert rig.status()["ends_on"] == "typed"
    run(rig, 3)
    assert rig.status()["session"] == "active"             # it was already there once: that is not the person typing
    rig.r.lines = [line("PS C: git status", 320, 800), line("PS C: git status", 320, 900)]
    run(rig, 5)
    out = rig.wait(0)
    assert out["session"] == "done" and out["events"][0] == {"event": "advanced", "step": 1, "by": "typed"}


def test_until_text_gone_after_and_manual(rig):
    rig.r.lines = [line("Cargando", 100, 100)]
    rig.show({"steps": [{"say": "Espera", "until": {"text": "Completado"}}, {"say": "Espera", "until": {"gone": "Completado"}},
                        {"say": "Un momento", "until": {"after": 2}}, "Respira"]})
    run(rig, 3)
    assert rig.status()["step"] == 1
    rig.r.lines = [line("Completado", 100, 100)]
    run(rig, 5)                                            # nothing moved on screen: it is read every few seconds anyway
    assert rig.status()["step"] == 2
    run(rig, 5)
    assert rig.status()["step"] == 2                       # still there
    rig.r.lines = []
    rig.f.show(paint((0.05, 0.08, 0.05, 0.02)))            # the word went away
    run(rig, 1.8)
    assert rig.status()["step"] == 3                       # read twice without it, a moment apart
    run(rig, 2.4)
    assert rig.status() == {"session": "active", "step": 4, "of": 4, "say": "Respira", "ends_on": "manual"}
    run(rig, 5)
    assert rig.status()["step"] == 4
    assert [e["by"] for e in rig.wait(0)["events"] if e["event"] == "advanced"] == ["text", "text", "time"]


def test_the_person_can_go_on_go_back_ask_for_help_write_and_stop(rig):
    with pytest.raises(BairdError):
        rig.user("next")
    rig.show({"steps": ["Uno", "Dos", "Tres"]})
    rig.user("next")
    rig.user("next")
    rig.user("back")
    assert rig.status()["step"] == 2
    rig.user("help", "no lo veo")
    assert rig.state()["session"]["asked_help"] is True
    rig.user("say", "mejor por el menú")
    out = rig.wait(0)
    assert [e["event"] for e in out["events"]] == ["advanced", "advanced", "back", "help", "say"]
    assert out["events"][3] == {"event": "help", "step": 2, "text": "no lo veo"} and out["events"][4]["text"] == "mejor por el menú"
    assert "needs you" in out["next"] and out["look"]      # eyes are needed: the picture comes with the answer
    assert rig.state()["session"]["asked_help"] is False
    rig.user("next")
    rig.user("next")
    assert rig.status()["session"] == "done"
    rig.user("back")
    now = rig.status()
    said = now.pop("unanswered")         # nobody answered the person yet: it rides in every answer until someone does
    assert now == {"session": "active", "step": 3, "of": 3, "say": "Tres", "ends_on": "manual"}
    assert said == [{"event": "help", "step": 2, "text": "no lo veo"}, {"event": "say", "text": "mejor por el menú"}]
    rig.user("stop")
    out = rig.wait(0)
    assert out["session"] == "stopped" and out["by"] == "user" and "look" not in out
    assert rig.e.demands == 0
    run(rig, 13)
    assert rig.state()["session"] is None and rig.wait(0)["session"] == "none"


def test_what_the_person_says_survives_a_call_that_was_cut_short(rig):
    rig.show({"steps": ["Uno", "Dos"]})
    rig.wait(0)
    rig.user("say", "espera, no veo el botón")
    gone = threading.Event()
    gone.set()                                             # the caller hung up while waiting
    assert rig.wait(30, gone=gone) == {"session": "active", "events": [], "abandoned": True}
    out = rig.wait(0)
    assert [e["event"] for e in out["events"]] == ["say"] and out["unanswered"] == [{"event": "say", "text": "espera, no veo el botón"}]
    out = rig.wait(0)                                      # and had that answer been lost too, the next one still says it
    assert out["events"] == [] and out["unanswered"][0]["text"] == "espera, no veo el botón" and "still waiting" in out["next"]
    out = rig.show({"message": "Está arriba a la derecha."})
    assert "unanswered" not in out and "unanswered" not in rig.wait(0)


def test_a_wait_of_nonsense_seconds_answers_at_once(rig):
    rig.show({"steps": ["Uno"]})
    started = time.monotonic()
    for bad in (float("nan"), -5, "mucho", None):
        assert rig.wait(bad)["session"] == "active"
    assert time.monotonic() - started < 1.0


def test_numbers_that_are_not_numbers_and_wrong_shapes_are_refused(rig):
    space = Space((W, H))
    nan = float("nan")
    for bad in ({"say": "x", "marks": 5}, {"say": "x", "marks": True}, {"say": "x", "click": {"find": "Ir", "dx": [1]}},
                {"say": "x", "click": {"find": "Ir", "dy": nan}}, {"say": "x", "type": ["a", "b"], "at": [10, 10]},
                {"say": "x", "until": {"after": nan}}, {"say": "x", "until": nan}, {"say": "x", "click": [10, 10], "label": {"a": 1}}):
        with pytest.raises(BairdError):
            clean_step(bad, space)
    assert clean_step({"say": "x", "timeout_s": nan}, space)["timeout_s"] == clean_step({"say": "x"}, space)["timeout_s"]
    assert "threshold" not in clean_step({"say": "x", "until": {"change": [0, 0, 10, 10], "threshold": nan}}, space)["until"]
    assert clean_step({"say": "x", "type": 2026, "at": [10, 10]}, space)["marks"][0]["text"] == "2026"


def test_how_the_answer_brings_the_picture_is_not_taken_for_a_look(rig):
    out = rig.show({"steps": [{"say": "x", "click": [100, 100]}], "look": "always"})
    assert out["session"] == "active"
    with pytest.raises(BairdError) as err:
        rig.show({"steps": [{"say": "x", "click": [100, 100]}], "look": "abc123"})
    assert "no look" in str(err.value)


def test_a_mark_picked_by_nearness_stays_on_its_pick(rig):
    rig.r.lines = SCREEN + [line("Guardar", 100, 100)]      # the plain label elsewhere; the button reads "Guardar(G)"
    rig.show({"steps": [{"say": "Guarda", "click": {"find": "Guardar", "near": [940, 707]}}]})
    where = rig.state()["session"]["step"]["marks"][0]
    assert where["x"] == pytest.approx(940 / W, abs=0.02) and where["y"] == pytest.approx(707 / H, abs=0.01)
    run(rig, 1.5)
    rig.f.show(paint((0.9, 0.0, 0.1, 0.1)))                # something moves: the text is read again
    run(rig, 3.0)
    mark = rig.state()["session"]["step"]["marks"][0]
    assert (mark["x"], mark["y"], mark["state"]) == (where["x"], where["y"], "ok")


def test_new_steps_replace_the_pending_ones_or_follow_them(rig):
    rig.show({"steps": ["Uno", "Dos", "Tres"]})
    rig.user("next")
    rig.show({"steps": ["B", "C"]})                        # the plan changed: Dos and Tres are dropped
    state = rig.state()["session"]
    assert [s["say"] for s in state["steps"]] == ["Uno", "B", "C"] and state["index"] == 1
    rig.show({"steps": ["D"], "replace": False})
    state = rig.state()["session"]
    assert [s["say"] for s in state["steps"]] == ["Uno", "B", "C", "D"] and state["step"]["say"] == "B"
    for _ in range(3):
        rig.user("next")
    assert rig.status()["session"] == "done"
    rig.show({"steps": ["E"]})                             # after the end, more steps go on in the same session
    assert rig.status() == {"session": "active", "step": 5, "of": 5, "say": "E", "ends_on": "manual"}


def test_a_look_sets_the_space_coordinates_are_read_in(rig):
    out = rig.look(max_side=960)
    assert out["space"] == [960, 540] and out["picture"] == [W, H] and out["_image"]["data"]
    rig.show({"steps": [{"say": "x", "click": [480, 270]}]})
    mark = rig.state()["session"]["step"]["marks"][0]
    assert (mark["x"], mark["y"]) == (0.5, 0.5)
    zoom = rig.look(region=[480, 270, 240, 135], text=True)     # the lower right quarter of the middle
    assert zoom["text"] == []                                   # none of the screen's texts are inside it
    rig.show({"steps": [{"say": "x", "click": [0, 0]}]})
    mark = rig.state()["session"]["step"]["marks"][0]
    assert (mark["x"], mark["y"]) == (0.5, 0.5)
    rig.show({"steps": [{"say": "x", "click": [960, 540]}], "space": [W, H]})
    assert rig.state()["session"]["step"]["marks"][0]["x"] == 0.5
    with pytest.raises(BairdError):
        rig.show({"steps": [{"say": "x", "click": [1, 1]}], "look": "nope"})
    assert rig.look_image(out["look"]) == b"\xff\xd8fake"


def test_texts_lists_and_finds(rig):
    rig.look(max_side=960)
    out = rig.texts()
    assert out["space"] == [960, 540] and "40,24 Archivo Editar Ver" in out["lines"]
    out = rig.texts("publicar rama")
    assert out["count"] == 1 and out["matches"][0]["at"] == pytest.approx([876, 44], abs=1) and out["matches"][0]["score"] == 1.0
    rig.r.works = False
    with pytest.raises(BairdError) as err:
        rig.texts("x")
    assert err.value.code == "unavailable"


def test_when_text_cannot_be_read_coordinates_still_work_and_the_assistant_is_told(rig):
    rig.r.works = False
    rig.show({"steps": [{"say": "x", "click": "Guardar"}]})
    assert rig.status()["searching"] == ["Guardar"]
    run(rig, 7)
    out = rig.wait(0)
    assert out["events"][0]["event"] == "anchor_missing" and "no OCR here" in out["events"][0]["why"]
    assert out["space"] == [1024, 576]                     # the answer brought a picture: its pixels are the coordinates now
    rig.show({"steps": [{"say": "x", "click": [512, 288]}]})
    run(rig, 1.5)
    rig.f.show(paint((0.45, 0.42, 0.12, 0.2)))
    run(rig, 1.2)
    assert rig.status()["session"] == "done"


def test_a_step_that_takes_too_long_and_a_session_nobody_touches(rig):
    rig.show({"steps": [{"say": "x", "until": "manual", "timeout_s": 10}]})
    run(rig, 11, step=0.5)
    assert events(rig) == ["stalled"]
    run(rig, 20, step=0.5)
    assert events(rig) == []                               # told once
    rig.t.now += DEFAULT_COPILOT["idle_minutes"] * 60
    rig._tick()
    out = rig.wait(0)
    assert out["session"] == "stopped" and out["by"] == "idle"


def test_turned_off_means_no_assistant_draws(rig):
    rig.settings["copilot"]["enabled"] = False
    for call in (lambda: rig.show({"steps": ["x"]}), rig.look, rig.texts):
        with pytest.raises(BairdError) as err:
            call()
        assert err.value.code == "unavailable" and "turn it on" in err.value.hint
    assert rig.state() == {"enabled": False, "rev": 0, "reads_text": True, "session": None}


def test_stop_by_the_assistant_leaves_a_last_line(rig):
    rig.show({"steps": ["x"]})
    assert rig.stop("Listo") == {"session": "stopped", "by": "agent"}
    state = rig.state()["session"]
    assert state["status"] == "stopped" and state["message"] == "Listo" and state["ended_by"] == "agent"
    assert rig.stop() == {"session": "none"}


def test_the_assistant_talks_and_the_conversation_is_kept(rig):
    out = rig.show({"agent": "Ana", "message": "Vamos a publicar la rama: son dos pasos."})
    assert out["session"] == "done" and out["of"] == 0             # nothing to do yet: it only spoke
    assert rig.state()["session"]["log"] == [{"id": 1, "who": "agent", "text": "Vamos a publicar la rama: son dos pasos."}]
    rig.show({"message": "Empezamos.", "steps": [{"say": "Uno", "hint": "Porque hace falta"}, "Dos"]})
    rig.user("next")
    rig.user("say", "¿y ahora?")
    rig.user("back")
    rig.user("next")                                               # going back does not say the step again
    rig.user("help")
    log = rig.state()["session"]["log"]
    assert [(e["who"], e["text"]) for e in log] == [("agent", "Vamos a publicar la rama: son dos pasos."), ("agent", "Empezamos."), ("step", "Uno"),
                                                    ("step", "Dos"), ("you", "¿y ahora?"), ("you", "No me sale este paso.")]
    assert log[2] == {"id": 3, "who": "step", "text": "Uno", "n": 1, "hint": "Porque hace falta", "done": True} and log[3]["done"] is False
    out = rig.wait(0)
    assert [e["event"] for e in out["events"]] == ["advanced", "say", "back", "advanced", "help"] and "answer with a message" in out["next"]
    rig.show({"message": "Mira el menú de arriba."})                # an answer, with the plan left as it is
    assert rig.status()["say"] == "Dos" and rig.state()["session"]["log"][-1]["text"] == "Mira el menú de arriba."
    rig.stop("Hasta luego")
    assert rig.state()["session"]["log"][-1] == {"id": 8, "who": "agent", "text": "Hasta luego"}
    with pytest.raises(BairdError):
        rig.show({})
