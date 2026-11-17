"""v2.1.1 Korean contracts, using synthetic values only."""
import hashlib
import json
import unittest

from bizcard_intake import contacts, parser
from bizcard_intake.vision_ocr import OcrLine, OcrResult


def korean_phone_corpus():
    groups = [(p, '5555', '0100') for p in
              ('010', '011', '016', '017', '018', '019', '02', '070', '080')
              + tuple(f'{n:03}' for n in range(31, 65))]
    corpus = ['', '1588-0100', '1577-0100', '1644-0100']
    for area, middle, end in groups:
        for separator in ('', '-', '.', ' ', '–'):
            value = separator.join((area, middle, end))
            corpus.extend((value, '  ' + value + '  ',
                           value.translate(str.maketrans('0123456789', '０１２３４５６７８９'))))
        corpus.extend((f'({area}) {middle}-{end}', f'+82 {area[1:]}-{middle}-{end}',
                       f'+82 (0){area[1:]}-{middle}-{end}', f'82-{area[1:]}-{middle}-{end}',
                       f'0082 {area[1:]} {middle} {end}'))
    return corpus


def digest(values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


class KoreanPreservationTest(unittest.TestCase):
    def test_normalize_phone_matches_v211_on_korean_corpus(self):
        self.assertEqual(digest([contacts.normalize_phone(v) for v in korean_phone_corpus()]),
                         '1cd8656b605b078ea6f0210a2d8e503698c82c4cfd3177f954b1150103731071')

    def test_international_phone_matches_v211_on_korean_corpus(self):
        self.assertEqual(digest([contacts.international_phone(v) for v in korean_phone_corpus()]),
                         'b8e852eb1dcbd168178a1d29d36b28128ec99f15ca77a66b52d3cf8da1d0129d')

    def test_korean_and_english_prompts_match_v211_bytes(self):
        # SHA-256 values were generated with git show v2.1.1:bizcard_intake/parser.py.
        cases = [('홍길동\n테스트 주식회사\n010-5555-0100',
          {'front': '846f4cd39dc179a741e1f85bdee0809cdbe6820d3f974605916393608fdca0da',
           'back': '9ab62a65f4e66257f576b6a4b4bb892f254baac61844aca28f32dedb559742ff'}),
         ('테스트 株式会社',
          {'front': '33d70b123887a72dd0602f59da72953b2824a992d721f4f3778dc3dac9c25bf2',
           'back': '57fb09cbbb76b60cf762b58456bc46d5e023bcf69ce94760e5e2b4c737a2dc09'}),
         ('ㄱ 株式会社',
          {'front': 'f4090fe52ef53981eb035a0ff890f5417caf6cade456d65b033297e9d53ba633',
           'back': '500b52c00ad887c60d8adbefd131f96ad7a35829c98fad69e442d7228807b83e'}),
         ('ᄀ 株式会社',
          {'front': '26d9edc7c6d9170a30c7ed1f2634adff0f7efd0f24e4c4fd0fea84d2fe854d83',
           'back': 'de76c14583dfd765365b156ec7ed02b2106766d386a6aa7563375a0cbdd1b25f'}),
         ('Synthetic Example Company',
          {'front': '9621c0ee5a71dd89c22c6b6a24661f636aa53e59cb9e89ca653a3791a58d2471',
           'back': 'b76426c541c11d58eb67da85e162409ba8894f0cbcfdb331585f60024b04ff93'}),
         ('',
          {'front': '76726e7d05890d93ab12dc55f356466292faaabbf2abdb017d6267e21ee05af4',
           'back': 'f8bd56ca349a704fe29371d6f6bf9dc813f2e75568d9fee4061ecb7bddf9355d'})]
        for text, expected in cases:
            for side, expected_hash in expected.items():
                with self.subTest(text=text, side=side):
                    ocr = OcrResult((OcrLine(text, .875),)) if text else OcrResult(())
                    self.assertEqual(hashlib.sha256(parser.card_prompt(ocr, side).encode()).hexdigest(),
                                     expected_hash)

    def test_only_kana_or_han_without_any_hangul_gets_cjk_guide(self):
        for text in ('株式会社テスト', '测试有限公司', '測試股份有限公司', 'やまだ', 'ﾃｽﾄ', '𠀀'):
            with self.subTest(text=text):
                prompt = parser.card_prompt(OcrResult((OcrLine(text, 1),)), 'front')
                self.assertIn('후리가나', prompt)
                self.assertIn('070/080', prompt)
                self.assertIn('국제 형식', prompt)
        for text in ('테스트 株式会社', 'ㄱ 株式会社', 'ᄀ 株式会社', 'Company +81 90-0000-0000', ''):
            with self.subTest(text=text):
                self.assertNotIn('후리가나', parser.card_prompt(OcrResult((OcrLine(text, 1),)), 'front'))

    def test_korean_full_width_title_slash_keeps_v211_display(self):
        from bizcard_intake.draft import CardDraft
        for title in ('팀장／컨설턴트', 'Sales／Director'):
            draft = CardDraft.from_dict({'name': '홍길동', 'title': title, 'company': '가상회사'})
            self.assertEqual(draft.display_name.encode(), f'홍길동 {title} 가상회사'.encode())
