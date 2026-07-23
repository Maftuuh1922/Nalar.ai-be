import re
from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT

def markdown_to_docx(markdown_text: str, output_path: str):
    """
    Converts simple markdown text (from STORM) to a neatly formatted .docx file.
    Supports headings (# to ####), bold (**text**), and basic lists.
    """
    document = Document()
    
    # Optional: adjust default font
    style = document.styles['Normal']
    font = style.font
    font.name = 'Arial'
    font.size = Pt(11)
    
    lines = markdown_text.split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        if line.startswith('# '):
            heading = document.add_heading(line[2:], level=1)
            heading.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        elif line.startswith('## '):
            document.add_heading(line[3:], level=2)
        elif line.startswith('### '):
            document.add_heading(line[4:], level=3)
        elif line.startswith('#### '):
            document.add_heading(line[5:], level=4)
        elif line.startswith('- ') or line.startswith('* '):
            document.add_paragraph(line[2:], style='List Bullet')
        else:
            # Handle basic bold tags if they exist inline
            p = document.add_paragraph()
            # Simple parser for **bold** text
            parts = re.split(r'(\*\*.*?\*\*)', line)
            for part in parts:
                if part.startswith('**') and part.endswith('**'):
                    p.add_run(part[2:-2]).bold = True
                else:
                    p.add_run(part)

    document.save(output_path)
    return output_path
