"""coverage の純粋関数の単体テスト。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import coverage


class BuildLabeledItemsTest(unittest.TestCase):

    def test_種別ごとに連番のIDを振る(self):
        items = coverage.build_labeled_items(
            messages=["散歩した", "本を読んだ"],
            gcal_events=["10:00 会議"],
            github_activity=["memoryex に 3 コミット"],
            url_summaries=[{"title": "記事タイトル", "url": "https://example.com"}],
            location_names=["京都駅"],
            movie_infos=[{"title": "映画A"}],
            health_data={"steps": 8000},
        )
        self.assertEqual(items["M1"], "メモ: 散歩した")
        self.assertEqual(items["M2"], "メモ: 本を読んだ")
        self.assertEqual(items["C1"], "予定: 10:00 会議")
        self.assertEqual(items["G1"], "GitHub: memoryex に 3 コミット")
        self.assertEqual(items["U1"], "共有URL: 記事タイトル")
        self.assertEqual(items["L1"], "訪問場所: 京都駅")
        self.assertEqual(items["V1"], "映画: 映画A")
        self.assertEqual(items["H1"], "健康: 歩数 8,000歩")

    def test_URLはタイトルが無ければURLを使う(self):
        items = coverage.build_labeled_items(
            [], [], [], [{"title": "", "url": "https://example.com"}], [], [], {})
        self.assertEqual(items["U1"], "共有URL: https://example.com")

    def test_健康データは存在する項目だけ並べる(self):
        items = coverage.build_labeled_items(
            [], [], [], [], [], [],
            {"steps": 8000, "sleep_minutes": 425, "distance_km": 5.2})
        self.assertEqual(items["H1"], "健康: 歩数 8,000歩")
        self.assertEqual(items["H2"], "健康: 睡眠時間 7時間5分")
        self.assertEqual(items["H3"], "健康: 移動距離 5.2km")
        self.assertNotIn("H4", items)

    def test_睡眠が丁度の時間なら分を省く(self):
        items = coverage.build_labeled_items([], [], [], [], [], [], {"sleep_minutes": 420})
        self.assertEqual(items["H1"], "健康: 睡眠時間 7時間")

    def test_データが無ければ空になる(self):
        self.assertEqual(coverage.build_labeled_items([], [], [], [], [], [], {}), {})


class FindMissingTest(unittest.TestCase):

    BODY = "<p>今日は鴨川沿いを一時間ほど散歩した。</p><p>夜は本を読んで過ごした。</p>"

    def test_本文に実在する抜粋なら反映済みとみなす(self):
        items = {"M1": "メモ: 散歩した"}
        got = coverage.find_missing(items, self.BODY, {"M1": "鴨川沿いを一時間ほど散歩した"})
        self.assertEqual(got, [])

    def test_タグをまたぐ抜粋も照合できる(self):
        items = {"M1": "メモ: 読書"}
        got = coverage.find_missing(items, self.BODY, {"M1": "散歩した。夜は本を読んで"})
        self.assertEqual(got, [])

    def test_空白の違いを無視する(self):
        items = {"M1": "メモ: 散歩"}
        got = coverage.find_missing(items, self.BODY, {"M1": "鴨川沿いを 一時間ほど 散歩した"})
        self.assertEqual(got, [])

    def test_本文に無い抜粋は未反映とする(self):
        items = {"M1": "メモ: 歯医者に行った"}
        got = coverage.find_missing(items, self.BODY, {"M1": "歯医者で治療を受けた"})
        self.assertEqual(got, ["メモ: 歯医者に行った"])

    def test_キーが無ければ未反映とする(self):
        items = {"M1": "メモ: 散歩", "M2": "メモ: 歯医者"}
        got = coverage.find_missing(items, self.BODY, {"M1": "鴨川沿いを一時間ほど散歩した"})
        self.assertEqual(got, ["メモ: 歯医者"])

    def test_短すぎる抜粋は未反映とする(self):
        items = {"M1": "メモ: 散歩"}
        got = coverage.find_missing(items, self.BODY, {"M1": "散歩"})
        self.assertEqual(got, ["メモ: 散歩"])

    def test_coverageが辞書でなければ全て未反映とする(self):
        items = {"M1": "メモ: 散歩", "M2": "メモ: 読書"}
        got = coverage.find_missing(items, self.BODY, None)
        self.assertEqual(got, ["メモ: 散歩", "メモ: 読書"])

    def test_項目が無ければ空を返す(self):
        self.assertEqual(coverage.find_missing({}, self.BODY, {}), [])


class StripHtmlTest(unittest.TestCase):

    def test_タグと空白を除去する(self):
        self.assertEqual(coverage.strip_html("<p>あ い</p>\n<p>う</p>"), "あいう")


if __name__ == "__main__":
    unittest.main()
