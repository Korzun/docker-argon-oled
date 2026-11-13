import pytest
from PIL import Image, ImageDraw
from luma.core.device import dummy
from luma.core.render import canvas

import pages
from containers import Container
from pages import PageContent, Snapshot
from state import PAGE_CONTAINERS, PAGE_NETWORK, PAGE_OVERVIEW, PAGE_SYSTEM
from sysinfo import HostInfo

HOST = HostInfo(hostname="pi-basement", ip="192.168.1.42", uptime_s=12 * 86400 + 3 * 3600 + 5)
STATS = {"cpu": 12.34, "temp": 45.67, "mem": 34.5, "disk": 56.7}


def c(name, running=True, health=None):
    return Container(name=name, running=running, health=health)


def snap(containers, stats=STATS, host=HOST):
    return Snapshot(stats=stats, containers=containers, host=host)


GOOD = [c("coolify", health="healthy"), c("oled"), c("postgres", health="healthy")]


def test_overview_all_ok():
    page = pages.build_page(PAGE_OVERVIEW, snap(GOOD))
    assert page == PageContent(
        title="ALL OK",
        counter="1/4",
        rows=["Temp    45.7C", "Uptime  12d 3h", "Ctrs    3 up, 0 bad", "IP      192.168.1.42"],
    )


def test_overview_with_bad_containers_title():
    cs = GOOD + [c("redis", health="unhealthy"), c("cache", health="unhealthy")]
    page = pages.build_page(PAGE_OVERVIEW, snap(cs))
    assert page.title == "! 2 BAD"
    assert "Ctrs    5 up, 2 bad" in page.rows


def test_overview_stopped_containers_are_not_counted_as_up():
    page = pages.build_page(PAGE_OVERVIEW, snap(GOOD + [c("job", running=False)]))
    assert "Ctrs    3 up, 0 bad" in page.rows


def test_overview_docker_unreachable_shows_na():
    page = pages.build_page(PAGE_OVERVIEW, snap(None))
    assert page.title == "OVERVIEW"
    assert "Ctrs    n/a" in page.rows


def test_overview_missing_temp_and_ip_show_na():
    page = pages.build_page(
        PAGE_OVERVIEW,
        snap(GOOD, stats={**STATS, "temp": None}, host=HostInfo("h", None, 60)),
    )
    assert "Temp    n/a" in page.rows
    assert "IP      n/a" in page.rows


def test_containers_sorted_bad_first_and_truncated_with_more_row():
    cs = [c(f"svc{i:02d}", health="healthy") for i in range(50)]
    cs.append(c("zz-bad", health="unhealthy"))
    cs.append(c("stopped-one", running=False))
    page = pages.build_page(PAGE_CONTAINERS, snap(cs))
    assert page.title == "CONTAINERS" and page.counter == "2/4"
    assert len(page.rows) == 4
    assert page.rows[0].startswith("zz-bad") and page.rows[0].endswith("bad")
    assert page.rows[3] == "+49 more"


def test_containers_short_list_shows_everything():
    page = pages.build_page(PAGE_CONTAINERS, snap(GOOD))
    # name left-aligned, status right-aligned, every row exactly COLS wide
    assert page.rows == ["coolify".ljust(18) + " ok", "oled".ljust(19) + " -", "postgres".ljust(18) + " ok"]
    assert all(len(row) == pages.COLS for row in page.rows)


def test_containers_long_and_non_ascii_names_truncated():
    cs = [c("a-very-long-container-name-indeed", health="healthy"), c("dätä-café-日本語", health="unhealthy")]
    page = pages.build_page(PAGE_CONTAINERS, snap(cs))
    assert all(len(row) <= pages.COLS for row in page.rows)
    assert all(row.isascii() for row in page.rows)


def test_containers_unknown_docker():
    page = pages.build_page(PAGE_CONTAINERS, snap(None))
    assert page.rows == ["Docker n/a"]


def test_network_page():
    page = pages.build_page(PAGE_NETWORK, snap(GOOD))
    assert (page.title, page.counter, page.rows) == ("NETWORK", "3/4", ["pi-basement", "192.168.1.42"])


def test_system_page():
    page = pages.build_page(PAGE_SYSTEM, snap(GOOD))
    assert page.title == "SYSTEM" and page.counter == "4/4"
    assert page.rows == ["CPU   12.3%", "Mem   34.5%", "Disk  56.7%", "Temp  45.7C"]


def test_alert_page_lists_only_bad_names():
    cs = GOOD + [c("redis", health="unhealthy")]
    page = pages.build_page(PAGE_CONTAINERS, snap(cs), alert=True)
    assert page == PageContent(title="! UNHEALTHY", counter="2/4", rows=["redis", "press = ack"])


def test_alert_page_many_bad_names_still_fits_four_rows():
    cs = [c(f"bad{i}", health="unhealthy") for i in range(7)]
    page = pages.build_page(PAGE_CONTAINERS, snap(cs), alert=True)
    assert len(page.rows) == 4
    assert page.rows == ["bad0", "bad1", "+5 more", "press = ack"]


def test_alert_page_with_docker_unreachable():
    page = pages.build_page(PAGE_CONTAINERS, snap(None), alert=True)
    assert page.rows == ["Docker n/a", "press = ack"]


def test_drift_offset_cycles_every_minute():
    assert pages.drift_offset(0) == (0, 0)
    assert pages.drift_offset(59.9) == (0, 0)
    assert pages.drift_offset(60) == (1, 0)
    assert pages.drift_offset(120) == (1, 1)
    assert pages.drift_offset(180) == (0, 1)
    assert pages.drift_offset(240) == (0, 0)


FULL = pages.build_page(PAGE_OVERVIEW, snap(GOOD))


def lit_box(content, offset=(0, 0)):
    image = Image.new("1", (256, 128), 0)
    pages.draw_page(ImageDraw.Draw(image), content, offset)
    return image, image.getbbox()


def test_draw_page_inverts_title_bar_and_draws_rows():
    device = dummy(width=pages.WIDTH, height=pages.HEIGHT)
    with canvas(device) as draw:
        pages.draw_page(draw, FULL)
    image = device.image.convert("L")
    title = image.crop((0, 0, pages.WIDTH, pages.TITLE_H))
    assert sum(1 for p in title.getdata() if p) > 0.5 * pages.WIDTH * pages.TITLE_H
    for i in range(4):
        top = pages.TITLE_H + 2 + i * pages.ROW_H
        band = image.crop((0, top, pages.WIDTH, top + pages.ROW_H))
        assert band.getbbox() is not None, f"nothing drawn in row {i}"


def test_draw_page_offset_shifts_pixels_by_one():
    base, _ = lit_box(FULL)
    shifted, _ = lit_box(FULL, (1, 1))
    assert base.getpixel((0, 0)) and not shifted.getpixel((0, 0))
    assert shifted.getpixel((1, 1))


@pytest.mark.parametrize("offset", [(0, 0), (1, 0), (1, 1), (0, 1)])
@pytest.mark.parametrize("page", range(4))
def test_draw_page_stays_inside_screen_for_every_page_and_offset(page, offset):
    content = pages.build_page(page, snap(GOOD + [c("x" * 40, health="unhealthy")]))
    _, box = lit_box(content, offset)
    assert box[0] >= 0 and box[1] >= 0
    assert box[2] <= pages.WIDTH + offset[0] and box[3] <= pages.HEIGHT


@pytest.mark.parametrize("offset", [(0, 0), (1, 1)])
def test_alert_page_stays_inside_screen(offset):
    cs = [c(f"{'n' * 21}{i}", health="unhealthy") for i in range(7)]
    content = pages.build_page(PAGE_CONTAINERS, snap(cs), alert=True)
    _, box = lit_box(content, offset)
    assert box[2] <= pages.WIDTH + offset[0] and box[3] <= pages.HEIGHT


def title_lit_fraction(content, inverted):
    device = dummy(width=pages.WIDTH, height=pages.HEIGHT)
    with canvas(device) as draw:
        pages.draw_page(draw, content, inverted=inverted)
    band = device.image.convert("L").crop((0, 0, pages.WIDTH, pages.TITLE_H - 1))
    return sum(1 for p in band.getdata() if p) / (pages.WIDTH * (pages.TITLE_H - 1))


def test_non_inverted_title_lights_far_fewer_pixels():
    assert title_lit_fraction(FULL, inverted=True) > 0.5
    assert title_lit_fraction(FULL, inverted=False) < 0.25


def test_non_inverted_title_still_has_a_separator_and_stays_inside_screen():
    content = pages.build_page(PAGE_CONTAINERS, snap([c("x" * 40, health="unhealthy")]), alert=True)
    image = Image.new("1", (256, 128), 0)
    pages.draw_page(ImageDraw.Draw(image), content, (1, 1), inverted=False)
    box = image.getbbox()
    assert box[2] <= pages.WIDTH + 1 and box[3] <= pages.HEIGHT
    rule_y = pages.TITLE_H + 1  # the 1 px rule sits just under the title band, shifted by the y offset
    assert all(image.getpixel((x, rule_y)) for x in range(2, 100))
