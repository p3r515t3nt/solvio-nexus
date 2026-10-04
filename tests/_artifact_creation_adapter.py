"""Synthetic provider-written program, never an installed production adapter."""

SOURCE = '''import base64, io, json, sys
import xlsxwriter
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.utils import simpleSplit
body = json.load(sys.stdin)
source = body['derived_source']['snapshot']
lines = [body['owner_objective'], *source['befunde'], *source['quellen']]
book_data = io.BytesIO()
with xlsxwriter.Workbook(book_data, {'in_memory':True, 'strings_to_urls':False}) as book:
    sheet = book.add_worksheet('Results')
    sheet.set_column(0, 0, 110)
    for index, line in enumerate(lines):
        sheet.write_string(index, 0, line)
pdf_data = io.BytesIO()
pdf = Canvas(pdf_data, pagesize=(595, 842))
y = 805
for line in lines:
    for part in simpleSplit(line, 'Helvetica', 10, 515):
        if y < 40:
            pdf.showPage()
            y = 805
        pdf.setFont('Helvetica', 10)
        pdf.drawString(40, y, part)
        y -= 14
pdf.save()
files = [{'name': name, 'mime_type': mime, 'content_b64':base64.b64encode(raw).decode()}
    for name, mime, raw in (
        ('Comparison.xlsx','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',book_data.getvalue()),
        ('Comparison.pdf','application/pdf',pdf_data.getvalue()))]
print(json.dumps({'version':1, 'files':files}))
'''
