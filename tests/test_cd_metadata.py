from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cd_metadata_test_target", ROOT / "deploy/ttv-cd/cd_metadata.py")
metadata = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metadata)


def candidate(video_id, title, *, channel="wowaka", views=123, duration=197):
    return {"video_id": video_id, "title": title, "channel": channel,
            "view_count": views, "duration": duration}


class CdMetadataTests(unittest.TestCase):
    def test_high_view_reaction_is_rejected_before_popularity_selection(self):
        rows = [candidate("vnw8zURAxkU", "Composer Song (Official Video)",
                          channel="Composer", views=1),
                candidate("OcZM_Szee44", "Song - Composer Vocaloid Reaction & Analysis",
                          channel="Listener", views=90_000_000, duration=1223)]
        result = metadata.search_metadata("Song", "Composer", candidates=rows)
        self.assertEqual(result["selected"]["video_id"], "vnw8zURAxkU")
        self.assertEqual(result["audit"]["accepted_candidates"], 1)
        self.assertTrue(result["audit"]["criteria"]["unrequested_discussion_formats_rejected"])
        reaction = result["candidates"][1]
        self.assertIn("unrequested_version_reaction", reaction["rejection_reasons"])
        self.assertIn("unrequested_version_analysis", reaction["rejection_reasons"])
        # No expected recording duration is needed to reject a discussion;
        # MIDI excerpts cannot supply the full recording's duration.
        self.assertNotIn("duration_mismatch", reaction["rejection_reasons"])

    def test_multilingual_discussion_labels_require_an_explicit_request(self):
        labels = [("Vocal Coach Reacts", "reaction"),
                  ("First-Time Hearing", "reaction"),
                  ("Reação", "reaction"), ("Reações", "reaction"),
                  ("Reagindo", "reaction"), ("Reacción", "reaction"),
                  ("Réaction", "reaction"), ("Reaktion", "reaction"),
                  ("Reazione", "reaction"), ("Реакция", "reaction"),
                  ("リアクション", "reaction"), ("反應", "reaction"),
                  ("리액션", "reaction"),
                  ("Commentary", "commentary"), ("Comentários", "commentary"),
                  ("Comentarios", "commentary"), ("Review", "commentary"),
                  ("解説", "commentary"), ("解说", "commentary"),
                  ("해설", "commentary"),
                  ("Performance Analysis", "analysis"), ("Análise", "analysis"),
                  ("Análisis", "analysis"), ("Analyse", "analysis"),
                  ("Analisi", "analysis"), ("Разбор", "analysis"),
                  ("考察", "analysis"), ("解析", "analysis"),
                  ("분석", "analysis")]
        for label, version in labels:
            with self.subTest(label=label):
                row = candidate("vnw8zURAxkU", "Composer Song [" + label + "]", channel="Composer")
                accepted, reasons = metadata.match_candidate("Song", "Composer", row)
                self.assertFalse(accepted)
                self.assertIn("unrequested_version_" + version, reasons)
                result = metadata.search_metadata("Song", "Composer", allow_versions=[version],
                                                  candidates=[row])
                self.assertEqual(result["audit"]["accepted_candidates"], 1)

    def test_discussion_words_in_the_requested_song_title_do_not_reject_it(self):
        for title in ("Reaction", "Chain Reaction", "Analysis", "Commentary", "Reação", "解析"):
            with self.subTest(title=title):
                result = metadata.search_metadata(title, "Composer", candidates=[
                    candidate("vnw8zURAxkU", "Composer " + title + " (Official Audio)", channel="Composer")])
                self.assertEqual(result["audit"]["accepted_candidates"], 1)
        result = metadata.search_metadata("Song (Reaction & Analysis)", "Composer", candidates=[
            candidate("vnw8zURAxkU", "Composer Song (Reaction & Analysis)", channel="Composer")])
        self.assertEqual(result["audit"]["accepted_candidates"], 1)

    def test_supplied_171936_snapshot_rejects_reaction_and_selects_official(self):
        # Exact 37-row snapshot from the user's later phone package. The old
        # process selected a 20-minute reaction while rejecting the official
        # recording for its conventional Rollin/ Rolling title spelling.
        rows = [
            ('vnw8zURAxkU', 'wowaka 『ローリンガール』feat. 初音ミク / wowaka - Rollin Girl (Official Video) ft. Hatsune Miku', 'ヒトリエ / wowaka', 44780292, 197),
            ('vPlFkyXY6L0', 'Rolling Girl (ローリンガール) feat. Hatsune Miku | Project DIVA Arcade Future Tone', 'RADART Inc', 12560292, 190),
            ('NIqm73xsias', 'Hatsune Miku - Rolling Girl PV (English Subs)', 'ghostsubs', 11449954, 200),
            ('2uhFdKPes9k', 'ローリンガール (Rollin Girl)┃Raon cover', 'Raon', 517013, 195),
            ('DuTdmMsPa1o', 'ヒトリエ/HITORIE - Rolling Girl (LIVE) [Sub. Español]', 'KuroShiro', 734092, 241),
            ('dtNirx14Aik', 'Rolling Girl (Wowaka) English Cover by Lollia feat. RichaadEB', 'Lollia', 7043141, 220),
            ('vId1r-9ydSw', 'ROLLING GIRL (ローリンガール) (feat. Evil Neuro)', 'OBKATIEKAT - Topic', 756111, 188),
            ('xPhPHO-sKlY', '【Rolling girl ローリンガール】Wowaka Tribute - Hatsune Miku Magical Mirai 2019 初音ミク「マジカルミライ 2019」', 'First sound of the future', 142202, 263),
            ('8e3EuFaQc7E', 'Rolling Girl (English Cover)【Will Stetson】「ローリンガール」', 'Will Stetson', 1252213, 200),
            ('Mhfo7AK2-Bc', '02. Rolling Girl ft. Hatsune Miku - Wowaka', 'Lucy -senpai', 124886, 190),
            ('MtrQokmWupo', 'Rollin Girl - Wowaka feat. Hatsune Miku (Cover by ZUMA)', 'NIJIIRO ZAMURAI', 3186545, 189),
            ('ULq7yS6dBDU', 'ローリンガール / rolling girl || wowaka || daycore / slowed', 'cloudgender_', 37663, 222),
            ('9Ct3iDST4vw', '【MIKU V4X】 Rolling Girl 【jychee】', 'jychee', 40220, 191),
            ('Y9Xd9n6H-e0', '【VOCALOID BRASIL】 ROLLING GIRL - PORTUGUÊS BRASIL - HATSUNE MIKU - ローリンガール - 初音ミク', 'Im G Felipe', 371432, 195),
            ('gv73K9_2Jfc', '【Hitorie】Rolling Girl (Wowaka Tribute)【English Sub】', 'kisu_odd', 114480, 446),
            ('snkF6k2h91I', 'Utsu-P - Rolling Girl [wowaka cover] feat.初音ミク', '鬱P / Utsu-P', 748431, 191),
            ('I8cK3G5O3rE', 'Rolling Girl    By: Hatsune Miku(Wowaka)', 'Latchezar Ivanov Dimitrov', 85685, 228),
            ('k_YJDyVqdN4', 'Rolling Girl (English Cover)【Kasane Teto】ローリンガール', 'KimaroArt', 464786, 194),
            ('Bv8d-XFfL4A', "Rollin' Girl (2016.2.18 at Shimokitazawa Garden)", 'ヒトリエ / wowaka', 49200, 253),
            ('iOtp-GQMpXg', "TETO's GROWL (ROLLING GIRL)【 HARD ROCK VER 】", 'Teto Chan!', 2869991, 205),
            ('HRMumWO9eb0', '【Karaoke】Rolling Girl【off vocal】 wowaka', 'rediakaikai', 1199060, 200),
            ('M91PPbY7o0E', 'Rolling Girl - Wowaka [tradução]', 'vivi', 2894, 191),
            ('FK0q0ynIMcw', 'Rolling Girl - Miku Rolling', 'cini smile', 1184015, 110),
            ('XolAj3fyz1E', 'Rolling Girl (Metal Cover)', 'Project Epsilon - Topic', 56624, 190),
            ('bDOQqafPh-k', '【2人 Duet 】Rolling Girl 「ローリンガール」[Wowaka] (Metal Cover) ft. @MamieMegu & @rachie', 'Project Epsilon and rachie 🎀💌', 51938, 192),
            ('yK3BqKXf568', '【Wowaka】Rolling Girl (Supernova Ver)【English Sub】', 'kisu_odd', 6895, 193),
            ('wC1iW6hBfuI', '[VOCALOID] Hatsune Miku Rolling Girl [Japanese Romanji English Lyrics]', 'Voca-Lyrics', 189952, 211),
            ('x2eNWxQ20wY', 'Hatsune Miku: Project DIVA F 2nd - [PV] "Rolling Girl" (DLC) (English Subs/Sub. Español)', "Karin's Channel", 225103, 199),
            ('q247Rpa32ao', '[Eng Sub] Rolling Girl - Vocaloid - Hatsune Miku (Live in Sapporo, Japan)', 'Jonny M', 410095, 191),
            ('-iZVhJ6NTvA', 'ローリンガール / wowaka - Covered by しほ', 'しほ / shiho', 2224716, 192),
            ('OcZM_Szee44', 'It’s Okay To Not Be Okay | Rolling Girl - Wowaka ft. Hatsune Miku Vocaloid Reaction & Analysis', 'shooshiMooshi', 216547, 1223),
            ('mdeg4an_5V8', "wowaka - Rolling Girl (Evil's sing) w/ Lyrics", 'Random Neuro', 8013, 197),
            ('4EzAaStLZco', 'Rolling Girl -𝗥𝗲𝘃𝗲𝗻𝗴𝗲 𝗩𝗲𝗿-  (English Cover)【JubyPhonic】ローリンガール', 'JubyPhonic', 259865, 192),
            ('Oup1Iioj_6g', 'ローリンガール/wowaka【Covered by七海うらら】', 'Nanami Urara', 3407031, 193),
            ('P-Qg-O-3gG4', '【ナノ】Nano - Rolling Girl', 'Mana', 1266605, 201),
            ('efMtEYKxK2c', 'Rolling girl lollia old ver', 'puruglys', 190345, 232),
            ('-F2VcAPRnSc', 'wowaka - Rolling Girl (by acane_madder)', 'SlatorXVertigo', 146755, 180),
        ]
        result = metadata.search_metadata("Rolling Girl", "wowaka", candidates=[
            candidate(video_id, title, channel=channel, views=views, duration=duration)
            for video_id, title, channel, views, duration in rows])
        self.assertEqual(result["selected"]["video_id"], "vnw8zURAxkU")
        self.assertEqual(result["selected"]["view_count"], 44780292)
        self.assertEqual(result["audit"]["inspected_candidates"], 37)
        self.assertEqual(result["audit"]["accepted_candidates"], 3)
        audited = {row["video_id"]: row for row in result["candidates"]}
        self.assertFalse(audited["OcZM_Szee44"]["matching"])
        self.assertIn("unrequested_version_reaction", audited["OcZM_Szee44"]["rejection_reasons"])
        self.assertIn("unrequested_version_analysis", audited["OcZM_Szee44"]["rejection_reasons"])
        self.assertTrue(audited["vnw8zURAxkU"]["matching"])
        self.assertFalse(result["audit"]["global_youtube_maximum_claimed"])

    def test_supplied_163225_snapshot_selects_official_without_title_aliases(self):
        # Complete 34-row search snapshot from the phone ZIP supplied by the
        # user. Preserve title/channel/count/duration evidence, without using
        # song-specific aliases or querying the provider during this test.
        rows = [
            ("vnw8zURAxkU", "wowaka 『ローリンガール』feat. 初音ミク / wowaka - Rollin Girl (Official Video) ft. Hatsune Miku", "ヒトリエ / wowaka", 44779745, 197),
            ("vPlFkyXY6L0", "Rolling Girl (ローリンガール) feat. Hatsune Miku | Project DIVA Arcade Future Tone", "RADART Inc", 12559993, 190),
            ("NIqm73xsias", "Hatsune Miku - Rolling Girl PV (English Subs)", "ghostsubs", 11449909, 200),
            ("DuTdmMsPa1o", "ヒトリエ/HITORIE - Rolling Girl (LIVE) [Sub. Español]", "KuroShiro", 734015, 241),
            ("Mhfo7AK2-Bc", "02. Rolling Girl ft. Hatsune Miku - Wowaka", "Lucy -senpai", 124886, 190),
            ("dtNirx14Aik", "Rolling Girl (Wowaka) English Cover by Lollia feat. RichaadEB", "Lollia", 7043057, 220),
            ("xPhPHO-sKlY", "【Rolling girl ローリンガール】Wowaka Tribute - Hatsune Miku Magical Mirai 2019 初音ミク「マジカルミライ 2019」", "First sound of the future", 142200, 263),
            ("2uhFdKPes9k", "ローリンガール (Rollin Girl)┃Raon cover", "Raon", 516952, 195),
            ("8e3EuFaQc7E", "Rolling Girl (English Cover)【Will Stetson】「ローリンガール」", "Will Stetson", 1252213, 200),
            ("snkF6k2h91I", "Utsu-P - Rolling Girl [wowaka cover] feat.初音ミク", "鬱P / Utsu-P", 748419, 191),
            ("MtrQokmWupo", "Rollin Girl - Wowaka feat. Hatsune Miku (Cover by ZUMA)", "NIJIIRO ZAMURAI", 3186492, 189),
            ("M91PPbY7o0E", "Rolling Girl - Wowaka [tradução]", "vivi", 2894, 191),
            ("Bv8d-XFfL4A", "Rollin' Girl (2016.2.18 at Shimokitazawa Garden)", "ヒトリエ / wowaka", 49200, 253),
            ("gv73K9_2Jfc", "【Hitorie】Rolling Girl (Wowaka Tribute)【English Sub】", "kisu_odd", 114480, 446),
            ("lPvbewhBD5g", "〈ミクの日〉 ローリンガール Rolling Girl", "那比 NABI", 1054170, 40),
            ("Y9Xd9n6H-e0", "【VOCALOID BRASIL】 ROLLING GIRL - PORTUGUÊS BRASIL - HATSUNE MIKU - ローリンガール - 初音ミク", "Im G Felipe", 371432, 195),
            ("E5IjK9Czp_8", "E.C.H.O Project - Rolling Girl ⁓ (wowaka) [Furry]", "Bear-kun", 59752, 195),
            ("wC1iW6hBfuI", "[VOCALOID] Hatsune Miku Rolling Girl [Japanese Romanji English Lyrics]", "Voca-Lyrics", 189952, 211),
            ("FK0q0ynIMcw", "Rolling Girl - Miku Rolling", "cini smile", 1184023, 110),
            ("yK3BqKXf568", "【Wowaka】Rolling Girl (Supernova Ver)【English Sub】", "kisu_odd", 6895, 193),
            ("P-Qg-O-3gG4", "【ナノ】Nano - Rolling Girl", "Mana", 1266605, 201),
            ("mdeg4an_5V8", "wowaka - Rolling Girl (Evil's sing) w/ Lyrics", "Random Neuro", 8013, 197),
            ("x2eNWxQ20wY", 'Hatsune Miku: Project DIVA F 2nd - [PV] "Rolling Girl" (DLC) (English Subs/Sub. Español)', "Karin's Channel", 225101, 199),
            ("ULq7yS6dBDU", "ローリンガール / rolling girl || wowaka || daycore / slowed", "cloudgender_", 37663, 222),
            ("I8cK3G5O3rE", "Rolling Girl    By: Hatsune Miku(Wowaka)", "Latchezar Ivanov Dimitrov", 85685, 228),
            ("GG5MLl7ax8c", "Wowaka tribute + Rolling Girl Hatsune Miku // Magical Mirai 2019", "Extrawhale17", 129134, 259),
            ("-iZVhJ6NTvA", "ローリンガール / wowaka - Covered by しほ", "しほ / shiho", 2224677, 192),
            ("q247Rpa32ao", "[Eng Sub] Rolling Girl - Vocaloid - Hatsune Miku (Live in Sapporo, Japan)", "Jonny M", 410095, 191),
            ("Lj3GCkAHmPo", "♫UNDERTALE ♫ || ローリンガール Rolling Frisk ＊中文字幕/Eng sub＊", "Light Adam", 3129516, 197),
            ("Oup1Iioj_6g", "ローリンガール/wowaka【Covered by七海うらら】", "Nanami Urara", 3406994, 193),
            ("4EzAaStLZco", "Rolling Girl -𝗥𝗲𝘃𝗲𝗻𝗴𝗲 𝗩𝗲𝗿-  (English Cover)【JubyPhonic】ローリンガール", "JubyPhonic", 259837, 192),
            ("efMtEYKxK2c", "Rolling girl lollia old ver", "puruglys", 190327, 232),
            ("i4S3PWDwK0U", "Miku & BoyFriend - Rolling Girl mod", "AnimeToons", 870567, 206),
            ("9RXWHjC9pvM", "【VOCALOID BRASIL】 ROLLING GIRL - HATSUNE MIKU, MEGURINE LUKA, KAGAMINE RIN & LEN, KAITO E MEIKO", "Im G Felipe", 215171, 191),
        ]
        result = metadata.search_metadata("Rolling Girl", "wowaka", candidates=[
            candidate(video_id, title, channel=channel, views=views, duration=duration)
            for video_id, title, channel, views, duration in rows])
        self.assertEqual(result["selected"]["video_id"], "vnw8zURAxkU")
        self.assertEqual(result["selected"]["view_count"], 44779745)
        self.assertEqual(result["audit"]["inspected_candidates"], 34)
        self.assertEqual(result["audit"]["accepted_candidates"], 3)
        self.assertEqual(result["audit"]["criteria"]["title_aliases"], ["Rolling Girl"])
        audited = {row["video_id"]: row for row in result["candidates"]}
        self.assertIn("unrequested_version_cover", audited["mdeg4an_5V8"]["rejection_reasons"])
        self.assertIn("unrequested_version_performer_variant", audited["mdeg4an_5V8"]["rejection_reasons"])
        self.assertIn("unrequested_version_live", audited["Bv8d-XFfL4A"]["rejection_reasons"])
        self.assertIn("unrequested_version_cover", audited["MtrQokmWupo"]["rejection_reasons"])
        self.assertFalse(result["audit"]["global_youtube_maximum_claimed"])

    def test_english_title_contractions_preserve_other_words_and_artist_identity(self):
        for wanted, found in (("Dancing Queen", "Dancin' Queen"),
                              ("Movin’ On", "Moving On"),
                              ("Walking Away", "Walkin Away")):
            with self.subTest(wanted=wanted, found=found):
                result = metadata.search_metadata(wanted, "Composer", candidates=[
                    candidate("vnw8zURAxkU", "Composer " + found, channel="Composer")])
                self.assertEqual(result["audit"]["accepted_candidates"], 1)
        for wanted, found in (("Rolling Girl", "Roll Girl"), ("Rolling Girl", "Rollin Girls"),
                              ("Rolling Girl", "Rolling Frisk"), ("King", "Kin"),
                              ("Thing", "Thin")):
            with self.subTest(wanted=wanted, found=found):
                accepted, reasons = metadata.match_candidate(wanted, "Composer", candidate(
                    "vnw8zURAxkU", "Composer " + found, channel="Composer"))
                self.assertFalse(accepted)
                self.assertIn("title_mismatch", reasons)
        accepted, reasons = metadata.match_candidate("Song", "Rolling", candidate(
            "vnw8zURAxkU", "Rollin Song", channel="Rollin"))
        self.assertFalse(accepted)
        self.assertIn("artist_mismatch", reasons)

    def test_possessive_sing_is_performer_label_but_bare_sing_is_a_title(self):
        for suffix in ("(Another Singer's sing)", "(Another Singer’s sing)"):
            with self.subTest(suffix=suffix):
                accepted, reasons = metadata.match_candidate("Song", "Composer", candidate(
                    "vnw8zURAxkU", "Composer Song " + suffix, channel="Composer"))
                self.assertFalse(accepted)
                self.assertIn("unrequested_version_cover", reasons)
                self.assertIn("unrequested_version_performer_variant", reasons)
        result = metadata.search_metadata("Sing", "Composer", candidates=[
            candidate("vnw8zURAxkU", "Composer Sing", channel="Composer")])
        self.assertEqual(result["audit"]["accepted_candidates"], 1)

    def test_real_search_snapshot_unrequested_arrangements_rejected(self):
        # Regression rows from the successful phone import: these were marked
        # matching by v8 despite explicitly identifying another performance.
        rows = [
            ("yK3BqKXf568", "【Wowaka】Rolling Girl (Supernova Ver)【English Sub】", "kisu_odd", 6895, 193, "version_variant"),
            ("egLnoM3d05U", "【DTXHD】 ローリンガール/wowoka(現実逃避P) 【初音ミク】", "ぬえトラ", 4563, 231, "instrumental"),
            ("Lf9vF7rJahk", "wowaka(現実逃避P) feat. 初音ミク (Hatsune Miku) - ローリンガール (Rolling Girl) (273 BPM)", "Viva Happy", 112, 138, "tempo_variant"),
            ("405JdMBMokA", "Sky 楽譜 - wowaka ( 現実逃避P ) / ローリンガール Rolling Girl", "Kuttari [くったり]", 7416, 71, "instrumental"),
            ("z37D13_aQBo", "ローリンガール/wowaka(現実逃避P) feat.初音ミク【オルゴール】", "R3 Music Box ~癒しのオルゴールサウンド~", 37274, 61, "instrumental"),
            ("sWxZZmzNn1o", "Sky 楽譜 - ローリンガール Rolling Girl / wowaka ( 現実逃避P )", "Kuttari [くったり]", 8017, 72, "instrumental"),
            ("-F2VcAPRnSc", "wowaka - Rolling Girl (by acane_madder)", "SlatorXVertigo", 146752, 180, "performer_variant"),
            ("8J3FQWw6IJQ", "Kanata Sings Rollin' Girl (ローリンガール) - wowaka 【Kanata 3DLIVE Graduation】", " The Icy Panda ʕ •ᴥ•ʔ ", 2711, 194, "cover"),
            ("ekCsMWWiLPs", "[]OpenUtau[] wowaka 『ローリンガール』feat. 幻音シエル / wowaka - Rollin Girl []UTAUCORD request[]", "あ", 78, 200, "cover"),
            ("WtVc0eKpQQ0", "2015米津玄师弹唱《Rolling Girl》wowaka ローリンガール（翻滚少女）", "我愛古裝音樂MV", 4943, 204, "cover"),
            ("jj42pSpiw0o", "ローリンガール - wowaka(現実逃避P)【Copy】", "2018 CoTT_K", 64, 187, "cover"),
            ("3cOeb9p5bfE", "【クラリネット】wowaka（現実逃避P）『ローリンガール』feat. 初音ミク", "pechaぺちゃ", 805, 191, "instrumental"),
        ]
        candidates = [candidate(video_id, title, channel=channel, views=views, duration=duration)
                      for video_id, title, channel, views, duration, _ in rows]
        # Low views intentionally prove that every incompatible candidate is
        # removed before popularity selection, without relying on the winner.
        candidates.append(candidate("vnw8zURAxkU", "wowaka 『ローリンガール』feat. 初音ミク / Rollin Girl (Official Video)",
                                    channel="ヒトリエ / wowaka", views=1, duration=197))
        result = metadata.search_metadata("Rolling Girl", "wowaka", aliases=["Rollin Girl", "ローリンガール"],
                                          artist_aliases=["現実逃避P"], candidates=candidates)
        self.assertEqual(result["selected"]["video_id"], "vnw8zURAxkU")
        self.assertEqual(result["audit"]["accepted_candidates"], 1)
        audited = {row["video_id"]: row for row in result["candidates"]}
        for video_id, _, _, _, _, version in rows:
            with self.subTest(video_id=video_id):
                self.assertFalse(audited[video_id]["matching"])
                self.assertIn("unrequested_version_" + version, audited[video_id]["rejection_reasons"])
        self.assertIn("unrequested_version_live", audited["8J3FQWw6IJQ"]["rejection_reasons"])

    def test_unknown_version_request_requires_matching_name(self):
        row = candidate("vnw8zURAxkU", "Composer Song (Supernova Ver)", channel="Composer")
        with self.assertRaises(metadata.MetadataNoMatchError):
            metadata.search_metadata("Song", "Composer", candidates=[row])
        for title, allowed in (("Song (Supernova Ver)", ()), ("Song", ("version_variant",))):
            with self.subTest(title=title):
                result = metadata.search_metadata(title, "Composer", allow_versions=allowed, candidates=[row])
                self.assertEqual(result["audit"]["accepted_candidates"], 1)
        with self.assertRaises(metadata.MetadataNoMatchError):
            metadata.search_metadata("Song (Album Ver)", "Composer", candidates=[row])
        with self.assertRaises(metadata.MetadataNoMatchError):
            metadata.search_metadata("Song (Other Supernova Ver)", "Composer", candidates=[row])

    def test_tempo_version_requires_matching_tempo_or_explicit_allowance(self):
        row = candidate("vnw8zURAxkU", "Composer Song (273 BPM)", channel="Composer")
        with self.assertRaises(metadata.MetadataNoMatchError):
            metadata.search_metadata("Song (180 BPM)", "Composer", candidates=[row])
        result = metadata.search_metadata("Song (273 BPM)", "Composer", candidates=[row])
        self.assertEqual(result["audit"]["accepted_candidates"], 1)
        result = metadata.search_metadata("Song (２７３.０BPM)", "Composer", candidates=[row])
        self.assertEqual(result["audit"]["accepted_candidates"], 1)
        result = metadata.search_metadata("Song", "Composer", allow_versions=["tempo_variant"], candidates=[row])
        self.assertEqual(result["audit"]["accepted_candidates"], 1)

    def test_performer_credit_is_not_satisfied_by_producer_credit(self):
        row = candidate("vnw8zURAxkU", "Composer Song (by Different Singer)", channel="Composer")
        with self.assertRaises(metadata.MetadataNoMatchError) as raised:
            metadata.search_metadata("Song", "Composer", candidates=[row])
        self.assertIn("unrequested_version_performer_variant", raised.exception.audit["candidates"][0]["rejection_reasons"])
        result = metadata.search_metadata("Song (by Different Singer)", "Composer", candidates=[row])
        self.assertEqual(result["audit"]["accepted_candidates"], 1)
        with self.assertRaises(metadata.MetadataNoMatchError):
            metadata.search_metadata("Song (by Another Singer)", "Composer", candidates=[row])

    def test_original_performer_alias_and_author_credits_are_not_variants(self):
        for suffix in ("(by Artist Alias)", "(written by Another Author)", "(lyrics by Another Author)",
                       "(music by Another Author)", "feat. Original Singer"):
            with self.subTest(suffix=suffix):
                result = metadata.search_metadata("Song", "Composer", artist_aliases=["Artist Alias"],
                    candidates=[candidate("vnw8zURAxkU", "Composer Song " + suffix, channel="Composer")])
                self.assertEqual(result["audit"]["accepted_candidates"], 1)

    def test_subtitle_pv_and_lyrics_presentations_still_match(self):
        for suffix in ("[English Sub]", "【Sub Español】", "【中文字幕修正】", "【オリジナルPV】",
                       "[Lyrics Ver]", "[English Sub Ver]", "(Official Version)"):
            with self.subTest(suffix=suffix):
                result = metadata.search_metadata("Song", "Composer", candidates=[
                    candidate("vnw8zURAxkU", "Composer Song " + suffix, channel="Composer")])
                self.assertEqual(result["audit"]["accepted_candidates"], 1)

    def test_multilingual_instruments_versions_and_known_allowance(self):
        for suffix, reason in (("【乐谱】", "instrumental"), ("【音樂盒】", "instrumental"),
                               ("【鋼琴】", "instrumental"), ("(Music Box)", "instrumental"),
                               ("(Version 2)", "version_variant"), ("[Off Vocal]", "karaoke"),
                               ("(彈唱)", "cover"), ("(3DLive)", "live")):
            with self.subTest(suffix=suffix):
                with self.assertRaises(metadata.MetadataNoMatchError) as raised:
                    metadata.search_metadata("Song", "Composer", candidates=[
                        candidate("vnw8zURAxkU", "Composer Song " + suffix, channel="Composer")])
                self.assertIn("unrequested_version_" + reason, raised.exception.audit["candidates"][0]["rejection_reasons"])
        result = metadata.search_metadata("Song", "Composer", allow_versions=["instrumental"], candidates=[
            candidate("vnw8zURAxkU", "Composer Song (Piano Version)", channel="Composer")])
        self.assertEqual(result["audit"]["accepted_candidates"], 1)

    def test_original_alias_and_japanese_survive_matching(self):
        selected = metadata.search_metadata("Rolling Girl", "wowaka", ["Rollin Girl", "ローリンガール"],
            candidates=[candidate("vnw8zURAxkU", "wowaka 『ローリンガール』feat. 初音ミク / Rollin Girl (Official Video)",
                                  channel="ヒトリエ / wowaka", views=44_107_888)])
        self.assertEqual(selected["selected"]["video_id"], "vnw8zURAxkU")
        self.assertIn("ローリンガール", metadata.unicode_key("『ローリンガール』"))
        self.assertFalse(selected["audit"]["global_youtube_maximum_claimed"])

    def test_japanese_only_title_and_artist(self):
        selected = metadata.search_metadata("ローリンガール", "ヒトリエ", candidates=[
            candidate("vnw8zURAxkU", "『ローリンガール』feat. 初音ミク", channel="ヒトリエ / wowaka")])
        self.assertEqual(selected["audit"]["accepted_candidates"], 1)

    def test_highest_views_among_identified_recording_versions(self):
        candidates = [candidate("vnw8zURAxkU", "wowaka Rolling Girl Official Video", views=700),
                      candidate("22222222222", "wowaka Rolling Girl Official Audio", views=800),
                      candidate("33333333333", "Rolling Girl wowaka cover", views=100000),
                      candidate("44444444444", "Other Song wowaka", views=200000),
                      candidate("55555555555", "Rolling Girl", channel="unrelated artist", views=300000)]
        selected = metadata.search_metadata("Rolling Girl", "wowaka", candidates=candidates)
        self.assertEqual(selected["selected"]["video_id"], "22222222222")
        self.assertEqual(selected["audit"]["rejected_candidates"], 3)

    def test_artist_alias_is_explicit_evidence(self):
        selected = metadata.search_metadata("Rolling Girl", "wowaka", artist_aliases=["ヒトリエ"],
            candidates=[candidate("vnw8zURAxkU", "Rolling Girl", channel="ヒトリエ")])
        self.assertEqual(selected["audit"]["accepted_candidates"], 1)

    def test_missing_artist_is_ambiguous(self):
        with self.assertRaises(metadata.MetadataNoMatchError) as raised:
            metadata.search_metadata("Rolling Girl", "", candidates=[candidate("vnw8zURAxkU", "Rolling Girl")])
        self.assertIn("artist_unspecified", raised.exception.audit["candidates"][0]["rejection_reasons"])

    def test_japanese_covers_and_english_covered_rejected(self):
        for suffix in ("歌ってみた", "Covered by Example", "カバー", "ライブ", "piano version"):
            with self.subTest(suffix=suffix):
                with self.assertRaises(metadata.MetadataNoMatchError):
                    metadata.search_metadata("Rolling Girl", "wowaka", candidates=[
                        candidate("vnw8zURAxkU", "wowaka Rolling Girl " + suffix)])

    def test_requested_variant_allowed(self):
        selected = metadata.search_metadata("Rolling Girl", "wowaka", allow_versions=["cover"],
            candidates=[candidate("vnw8zURAxkU", "wowaka Rolling Girl cover")])
        self.assertEqual(selected["audit"]["accepted_candidates"], 1)

    def test_live_tribute_previously_selected_for_rolling_girl_is_rejected(self):
        selected = metadata.search_metadata("Rolling Girl", "wowaka", aliases=["Rollin Girl", "ローリンガール"],
            candidates=[candidate("xPhPHO-sKlY", "[Rolling girl ローリンガール] Wowaka Tribute - Hatsune Miku Magical Mirai 2019", views=142072),
                        candidate("vnw8zURAxkU", "wowaka - Rollin Girl (Official Video)", views=44_107_888)])
        rejected = selected["audit"]["candidates"][0]
        self.assertIn("unrequested_version_live", rejected["rejection_reasons"])
        self.assertIn("unrequested_version_tribute", rejected["rejection_reasons"])
        self.assertEqual(selected["selected"]["video_id"], "vnw8zURAxkU")

    def test_lyrics_is_presentation_and_player_accent_normalization_reused(self):
        selected = metadata.search_metadata("Canção", "Artísta", candidates=[
            candidate("vnw8zURAxkU", "Artista Cancao [Lyrics]")])
        self.assertEqual(selected["audit"]["accepted_candidates"], 1)

    def test_unknown_and_boolean_views_cannot_win(self):
        rows = [candidate("vnw8zURAxkU", "wowaka Rolling Girl", views=None),
                candidate("22222222222", "wowaka Rolling Girl", views=True)]
        with self.assertRaises(metadata.MetadataNoMatchError) as raised:
            metadata.search_metadata("Rolling Girl", "wowaka", candidates=rows)
        self.assertEqual(raised.exception.audit["inspected_candidates"], 2)

    def test_queries_cover_aliases_and_deduplicate_same_video(self):
        calls = []
        def backend(query, **kwargs):
            calls.append((query, kwargs))
            return [candidate("vnw8zURAxkU", "wowaka ローリンガール", views=700)]
        selected = metadata.search_metadata("Rolling Girl", "wowaka", aliases=["ローリンガール"],
                                            limit=25, search_backend=backend)
        self.assertEqual(len(calls), 2)
        self.assertEqual(selected["audit"]["inspected_candidates"], 1)
        self.assertEqual(selected["audit"]["queries"][0]["limit"], 25)

    def test_provider_errors_do_not_include_credentials(self):
        def backend(query, **kwargs):
            raise RuntimeError("https://provider.invalid?key=SUPER_SECRET")
        with self.assertRaises(metadata.MetadataSearchError) as raised:
            metadata.search_metadata("Rolling Girl", "wowaka", search_backend=backend)
        self.assertNotIn("SUPER_SECRET", json.dumps(raised.exception.audit))
        self.assertEqual(raised.exception.audit["queries"][0]["error"], "RuntimeError")

    def test_api_reuses_player_and_fetches_statistics_at_requested_limit(self):
        calls = []
        class Response:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return json.dumps(self.body).encode()
        def fetch(request, timeout):
            parsed = urlparse(request.full_url)
            params = parse_qs(parsed.query)
            calls.append((parsed.path, params))
            if parsed.path.endswith("search"):
                return Response({"items": [{"id": {"videoId": "vnw8zURAxkU"}, "snippet": {
                    "title": "wowaka Rolling Girl", "channelTitle": "wowaka", "thumbnails": {}}}]})
            return Response({"items": [{"id": "vnw8zURAxkU", "statistics": {"viewCount": "800"},
                                       "contentDetails": {"duration": "PT3M17S"}, "status": {}}]})
        with patch.object(metadata, "urlopen", fetch):
            result = metadata.search_metadata("Rolling Girl", "wowaka", youtube_api_key="SUPER_SECRET", limit=25)
        self.assertEqual(result["selected"]["view_count"], 800)
        self.assertEqual(calls[0][1]["maxResults"], ["25"])
        self.assertEqual(calls[0][1]["order"], ["viewCount"])
        self.assertIn("statistics", calls[1][1]["part"][0])
        self.assertNotIn("SUPER_SECRET", json.dumps(result))
        self.assertFalse(any(name.startswith("cogs.musica.configuracao") for name in sys.modules))

    def test_standalone_bundle_loads_only_copied_sources(self):
        import shutil
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            shutil.copyfile(ROOT / "deploy/ttv-cd/cd_metadata.py", target / "cd_metadata.py")
            for name in metadata.PLAYER_SOURCES:
                path = target / "player_metadata" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                source = ROOT / "cogs/musica"
                if not (source / name).is_file():
                    source = ROOT / "deploy/ttv-cd/player_metadata"
                shutil.copyfile(source / name, path)
            spec = importlib.util.spec_from_file_location("isolated_cd_metadata", target / "cd_metadata.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            result = module.search_metadata("Rolling Girl", "wowaka", candidates=[
                candidate("vnw8zURAxkU", "wowaka Rolling Girl")])
            self.assertEqual(result["selected"]["video_id"], "vnw8zURAxkU")


if __name__ == "__main__":
    unittest.main()
