import io
import unittest
import zipfile

from financial_parser import extract_financials_from_zip_bytes, parse_numeric


class FinancialParserTests(unittest.TestCase):
    def test_extracts_nested_ixbrl_metrics(self):
        xhtml = '''
        <html xmlns="http://www.w3.org/1999/xhtml"
              xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
              xmlns:xbrli="http://www.xbrl.org/2003/instance"
              xmlns:se-ar-base="http://www.far.se/se/fr/ar/base/2020-12-01"
              xmlns:se-gen-base="http://www.taxonomier.se/se/fr/gen-base/2020-12-01">
          <body>
            <xbrli:context id="PERIOD0">
              <xbrli:entity><xbrli:identifier scheme="test">5560187493</xbrli:identifier></xbrli:entity>
              <xbrli:period><xbrli:endDate>2025-06-30</xbrli:endDate></xbrli:period>
            </xbrli:context>
            <ix:nonFraction name="se-gen-base:Nettoomsattning" contextRef="PERIOD0" decimals="0">25000000</ix:nonFraction>
            <ix:nonFraction name="se-gen-base:ResultatEfterFinansiellaPoster" contextRef="PERIOD0">1250000</ix:nonFraction>
            <ix:nonFraction name="se-gen-base:Tillgangar" contextRef="PERIOD0">50000000</ix:nonFraction>
            <ix:nonFraction name="se-gen-base:Kundfordringar" contextRef="PERIOD0">3000000</ix:nonFraction>
            <ix:nonFraction name="se-gen-base:Kassamedel" contextRef="PERIOD0">800000</ix:nonFraction>
            <ix:nonFraction name="se-gen-base:KortfristigaSkulder" contextRef="PERIOD0">7000000</ix:nonFraction>
            <ix:nonFraction name="se-gen-base:Aktiekapital" contextRef="PERIOD0">1000000</ix:nonFraction>
            <se-ar-base:Organisationsnummer>5560187493</se-ar-base:Organisationsnummer>
          </body>
        </html>
        '''

        inner_buffer = io.BytesIO()
        with zipfile.ZipFile(inner_buffer, 'w') as inner_zip:
            inner_zip.writestr('report.xhtml', xhtml)

        outer_buffer = io.BytesIO()
        with zipfile.ZipFile(outer_buffer, 'w') as outer_zip:
            outer_zip.writestr('inner.zip', inner_buffer.getvalue())

        records = extract_financials_from_zip_bytes(outer_buffer.getvalue())
        self.assertTrue(records)
        record = records[0]
        self.assertEqual(record['orgnr'], '5560187493')
        self.assertEqual(record['year'], 2025)
        self.assertEqual(record['omsattning'], 25000000.0)
        self.assertEqual(record['resultat'], 1250000.0)
        self.assertEqual(record['balansomslutning'], 50000000.0)
        self.assertEqual(record['kundfordringar'], 3000000.0)
        self.assertEqual(record['kassalikviditet'], 800000.0)
        self.assertEqual(record['kortfristiga_skulder'], 7000000.0)

    def test_parse_numeric_handles_swedish_and_us_formats(self):
        self.assertEqual(parse_numeric('1 234 567,89'), 1234567.89)
        self.assertEqual(parse_numeric('1.234.567,89'), 1234567.89)
        self.assertEqual(parse_numeric('1,234.56'), 1234.56)
        self.assertEqual(parse_numeric('-1.234,50'), -1234.5)
        self.assertEqual(parse_numeric('12.5%'), 12.5)


if __name__ == '__main__':
    unittest.main()
