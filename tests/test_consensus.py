from mathocr.stages.transcribe import WireLine, WireAlt, WireSpan, consensus


def test_disagreement_becomes_uncertainty():
    a = [WireLine(bbox=[100, 100, 900, 150], text=r"$= n^2 + (2n+1)$", confidence=0.9)]
    b = [WireLine(bbox=[110, 102, 880, 152], text=r"$= n^2 + (2n-1)$", confidence=0.8)]
    c = [WireLine(bbox=[100, 98, 905, 149], text=r"$= n^2 + (2n+1)$", confidence=0.85)]
    lines, uncs = consensus(1, {"A": a, "B": b, "C": c}, 1000, 1000)
    assert len(lines) == 1
    assert len(uncs) == 1
    u = uncs[0]
    assert u.chosen == "+"
    assert [r.text for r in u.readings] == ["+", "-"]
    assert u.readings[0].support == ["A", "C"] and u.readings[1].support == ["B"]
    assert lines[0].confidence < 0.9


def test_agreement_no_uncertainty_and_self_reported_span():
    a = [WireLine(bbox=[0, 0, 500, 50], text="$0^2 = 0$", confidence=0.9,
                  uncertain=[WireSpan(span="0^2", alternatives=[WireAlt(text="0^2", probability=0.8),
                                                                  WireAlt(text=r"0^\ell", probability=0.2)],
                                      reason="exposant en boucle")])]
    b = [WireLine(bbox=[0, 0, 500, 50], text="$0^{2} = 0$", confidence=0.9)]
    lines, uncs = consensus(1, {"A": a, "B": b}, 1000, 1000)
    assert len(uncs) == 1  # « 0^{2} » et « 0^2 » : même lecture ; reste l'incertitude déclarée
    assert any(u.span == "0^2" and u.readings[1].text == r"0^\ell" for u in uncs)


def test_line_missed_by_anchor_is_kept_with_low_confidence():
    a = [WireLine(bbox=[0, 0, 500, 50], text="Donc", confidence=0.9)]
    b = [WireLine(bbox=[0, 0, 500, 50], text="Donc", confidence=0.9),
         WireLine(bbox=[0, 600, 500, 650], text="$x = 1$", confidence=0.9)]
    lines, _ = consensus(1, {"A": a, "B": b}, 1000, 1000)
    assert len(lines) == 2
    assert lines[1].confidence < 0.5


def test_outvoted_anchor_is_replaced_by_majority_in_line_text():
    a = [WireLine(bbox=[100, 100, 900, 150], text=r"$= n^2 + (2n-1)$", confidence=0.9)]
    b = [WireLine(bbox=[100, 100, 900, 150], text=r"$= n^2 + (2n+1)$", confidence=0.9)]
    c = [WireLine(bbox=[100, 100, 900, 150], text=r"$= n^2 + (2n+1)$", confidence=0.9)]
    lines, uncs = consensus(1, {"A": a, "B": b, "C": c}, 1000, 1000)
    assert "(2n+1)" in lines[0].text
    assert uncs[0].chosen == "+" and uncs[0].readings[1].text == "-"


def test_letter_adjudication_renames_whole_line():
    from mathocr.stages.transcribe import _rename_letter
    assert _rename_letter(r"c-à-d $\sum_{k=0}^{n-1} (2k+1) = n^2$", "k", "h") == r"c-à-d $\sum_{h=0}^{n-1} (2h+1) = n^2$"
    assert _rename_letter(r"$\ker k$ et k", "k", "h") == r"$\ker h$ et k"


def test_mixed_box_conventions_are_repaired_per_box():
    from mathocr.stages.transcribe import normalize_boxes
    # Réponse réelle de Gemini : boîte 1 en [x0,y0,x1,y1], boîte 2 en [y0,x0,x1,y1]
    lines = [WireLine(bbox=[168, 142, 206, 163], text="Ex.", confidence=0.9),
             WireLine(bbox=[203, 298, 608, 245], text="$P(n)$", confidence=0.9)]
    fixed, conform = normalize_boxes(lines)
    assert fixed[0].bbox == [168, 142, 206, 163]
    assert fixed[1].bbox == [298, 203, 608, 245]
    assert conform == 0.5


def test_pixel_boxes_are_recalibrated_on_ink():
    """Un moteur qui renvoie des pixels (petite photo) au lieu de 0–1000 : l'encre tranche."""
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (960, 1280), "white")
    d = ImageDraw.Draw(im)
    rows = [(150, 180), (300, 330), (450, 480), (600, 630), (750, 780)]
    for y0, y1 in rows:
        for x in range(200, 700, 14):
            d.rectangle([x, y0, x + 7, y1], fill="navy")
    norm = [WireLine(bbox=[int(200 / .96), int(y0 / 1.28), int(700 / .96), int(y1 / 1.28)], text=f"ligne {i}",
                     confidence=0.9) for i, (y0, y1) in enumerate(rows)]
    pix = [WireLine(bbox=[200, y0, 700, y1], text=f"ligne {i}", confidence=0.9) for i, (y0, y1) in enumerate(rows)]
    notes = {}
    lines, uncs = consensus(1, {"A": norm, "B": pix}, 960, 1280, im=im, notes=notes)
    assert notes == {"B": "boîtes recalibrées (pixels 960x1280)"}
    assert len(lines) == 5 and all(len(ln.engine_readings) == 2 for ln in lines) and not uncs


def test_split_line_is_not_duplicated():
    a = [WireLine(bbox=[100, 100, 900, 160], text=r"on a $\gcd(a,b)=1 \Rightarrow d=d_1 d_2$", confidence=0.9)]
    b = [WireLine(bbox=[100, 100, 450, 160], text=r"on a $\gcd(a,b)=1$", confidence=0.9),
         WireLine(bbox=[460, 100, 900, 160], text=r"$\Rightarrow d=d_1 d_2$", confidence=0.9),
         WireLine(bbox=[100, 600, 500, 650], text="$x = 1$", confidence=0.9)]
    lines, _ = consensus(1, {"A": a, "B": b}, 1000, 1000)
    assert [ln.text for ln in lines][1:] == ["$x = 1$"]  # le vrai manque reste signalé


def test_layout_only_divergence_is_not_a_doubt():
    a = [WireLine(bbox=[100, 100, 900, 160], text=r"$\begin{cases} x=1 & \to 0 \end{cases}$", confidence=0.9)]
    b = [WireLine(bbox=[100, 100, 900, 160], text=r"$\begin{cases} x=1 \to 0 \end{cases}$", confidence=0.9)]
    _, uncs = consensus(1, {"A": a, "B": b}, 1000, 1000)
    assert uncs == []
    c = [WireLine(bbox=[100, 100, 900, 160], text=r"$x^{n+1}$", confidence=0.9)]
    d = [WireLine(bbox=[100, 100, 900, 160], text=r"$x^n+1$", confidence=0.9)]
    _, uncs = consensus(1, {"C": c, "D": d}, 1000, 1000)
    assert uncs  # les accolades changent le sens : le doute reste
