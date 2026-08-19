import re
import html


def get_sections(text):
    """Split the Form 731 HTML into {legend_text: chunk_html} in document order."""
    matches = list(re.finditer(r'<legend class="small-blue-content">(.*?)</legend>', text, re.S))
    sections = {}
    for i, m in enumerate(matches):
        name = re.sub(r'\s+', ' ', html.unescape(m.group(1))).strip().rstrip(':')
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[name] = text[start:end]
    return sections


def _clean(raw):
    text = html.unescape(re.sub(r'<[^>]+>', ' ', raw))
    return re.sub(r'\s+', ' ', text).strip()


def td_value(chunk, label_substr):
    pat = re.compile(
        r'<td[^>]*small-bold-content[^>]*>\s*' + re.escape(label_substr) +
        r'.*?</td>\s*<td[^>]*small-content[^>]*>(.*?)</td>', re.S | re.I)
    m = pat.search(chunk)
    return _clean(m.group(1)) if m else ''


def span_value(chunk, label_substr):
    pat = re.compile(
        r'<span[^>]*small-bold-content[^>]*>.*?' + re.escape(label_substr) +
        r'.*?</span>.*?<span[^>]*small-content[^>]*>(.*?)</span>', re.S | re.I)
    m = pat.search(chunk)
    return _clean(m.group(1)) if m else ''


def either(chunk, label):
    return span_value(chunk, label) or td_value(chunk, label)


def parse_contact(chunk):
    first = either(chunk, 'First Name')
    last = either(chunk, 'Last Name')
    firm = either(chunk, 'Firm Name')
    email = either(chunk, 'E-Mail') or either(chunk, 'E-mail') or either(chunk, 'Email')
    phone = either(chunk, 'Telephone Number')
    name = ' '.join(p for p in [first, last] if p)
    parts = [p for p in [name, firm, email, phone] if p]
    return ', '.join(parts)


def parse_application_info(text):
    sections = get_sections(text)
    info = {}

    info['tcb_email'] = td_value(sections.get('TCB Information', ''), 'TCB Application Email Address')
    info['tcb_scope'] = td_value(sections.get('TCB Information', ''), 'TCB Scope')

    info['responsible_party'] = parse_contact(sections.get("Person at the applicant's address to receive grant or for contact", ''))
    info['technical_contact'] = parse_contact(sections.get('Technical Contact', ''))
    info['non_technical_contact'] = parse_contact(sections.get('Non Technical Contact', ''))
    info['test_firm'] = parse_contact(sections.get('Test Firm Information', ''))

    info['long_term_confidential'] = span_value(sections.get('Long-Term Confidentiality', ''), 'confidentiality for any portion')
    info['short_term_confidential'] = span_value(sections.get('Short-Term Confidentiality', ''), 'Does short-term confidentiality apply')
    info['short_term_release_date'] = span_value(sections.get('Short-Term Confidentiality', ''), 'specify the short-term')

    info['cognitive_radio'] = span_value(sections.get('Software Defined/Cognitive Radio', ''), 'Is this application for software defined')
    info['modular_type'] = span_value(sections.get('Modular Equipment', ''), 'Modular Type')
    info['composite_device'] = span_value(sections.get('Composite/Related Equipment', ''), 'Is the equipment in this application a composite device')

    info['authorization_waiver'] = span_value(sections.get('Equipment Authorization Waiver', ''), 'Is there an equipment authorization waiver')
    info['authorization_waiver_approved'] = span_value(sections.get('Equipment Authorization Waiver', ''), 'If there is an equipment authorization waiver')

    equip_chunk = sections.get('Equipment Class', '')
    info['equipment_class'] = span_value(equip_chunk, 'Equipment Class:')
    info['device_description'] = td_value(equip_chunk, 'Description of product')

    # Columns: Line Entry, Lower Freq, Upper Freq, Power Output, Tolerance,
    # Emission Designator, Microprocessor Number, Rule Parts, Grant Notes.
    specs = []
    specs_chunk = sections.get('Equipment Specifications', '')
    rows = re.findall(r'<tr>(.*?)</tr>', specs_chunk, re.S)
    for row in rows:
        cells = [_clean(c) for c in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
        if len(cells) >= 8 and re.match(r'^\d+$', cells[0] or ''):
            specs.append({
                'lower_freq': cells[1], 'upper_freq': cells[2],
                'power_output': cells[3], 'tolerance': cells[4],
                'emission_designator': cells[5], 'rule_parts': cells[7],
            })
    info['equipment_specs'] = specs

    info['section_5301_certification'] = span_value(
        sections.get('SECTION 5301 (ANTI-DRUG ABUSE) CERTIFICATION', ''),
        'Does the applicant or authorized agent so certify')
    info['kdb_inquiry'] = span_value(
        sections.get('Related OET KnowledgeDataBase Inquiry', ''),
        'Is there a KDB inquiry associated with this application')

    return info


def parse_grant_text(html_text):
    """Tcb731GrantForm.cfm?mode=COPY -> the rendered grant certificate,
    matching fccid.io's "Grants" section. Returns cleaned plain text
    (line breaks preserved, tags stripped)."""
    m = re.search(r'<body[^>]*>(.*?)</body>', html_text, re.S | re.I)
    body = m.group(1) if m else html_text
    body = re.sub(r'<script.*?</script>', ' ', body, flags=re.S | re.I)
    body = re.sub(r'<style.*?</style>', ' ', body, flags=re.S | re.I)
    body = re.sub(r'<(br|/tr|/p|/div)[^>]*>', '\n', body, flags=re.I)
    body = html.unescape(re.sub(r'<[^>]+>', ' ', body))
    lines = [re.sub(r'[ \t]+', ' ', ln).strip() for ln in body.splitlines()]
    lines = [ln for ln in lines if ln]
    return '\n'.join(lines)


if __name__ == '__main__':
    import json
    text = open('form731.html', encoding='utf-8').read()
    info = parse_application_info(text)
    print(json.dumps(info, indent=2, ensure_ascii=False))
