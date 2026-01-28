import os
import json
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, date
from config import GOOGLE_SPREADSHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON, GOOGLE_SHEET_NAME

logger = logging.getLogger(__name__)

def _build_clients():
    """Return (gspread_client, sheets_api_service) or (None, None) on failure."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        if not GOOGLE_SERVICE_ACCOUNT_JSON:
            logger.error('Environment variable GOOGLE_SERVICE_ACCOUNT_JSON not set')
            return None, None

        creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=[
            'https://www.googleapis.com/auth/spreadsheets'
        ])

        gs = gspread.authorize(creds)
        sheets_service = build('sheets', 'v4', credentials=creds)
        return gs, sheets_service
    except Exception as e:
        logger.exception('Failed to build Google Sheets clients: %s', e)
        return None, None


def ensure_header(sheet):
    # Ensure header row exists
    try:
        values = sheet.row_values(1)
        if not values:
            header = ['date', 'store', 'total_amount', 'category', 'flag_needs_fix']
            sheet.insert_row(header, index=1)
    except Exception:
        pass


def append_receipt_row(data: Dict[str, Any]):
    """
    Append a receipt row to the configured Google Sheet.
    `data` is expected to contain at least keys: date, store, total_amount, category.
    Returns True on success, False otherwise.
    """
    gs, sheets_api = _build_clients()
    if not gs:
        return False

    try:
        sh = gs.open_by_key(GOOGLE_SPREADSHEET_ID)

        # Decide sheet title based on the provided date (monthly sheets)
        sheet_title = _sheet_title_for_data_date(data.get('date'))

        try:
            worksheet = sh.worksheet(sheet_title)
        except Exception:
            worksheet = sh.add_worksheet(title=sheet_title, rows='1000', cols='20')

        ensure_header(worksheet)

        # build row matching header (no raw_json column)
        row = [
            data.get('date', ''),
            data.get('store', ''),
            data.get('total_amount', ''),
            data.get('category', ''),
            'TRUE' if data.get('flag_needs_fix') else ''
        ]

        # Append using USER_ENTERED so Sheets will parse the date into a proper date cell
        try:
            worksheet.append_row(row, value_input_option='USER_ENTERED')
        except TypeError:
            worksheet.append_row(row, 'USER_ENTERED')

        # post-process: apply formatting and validation using Sheets API (best-effort)
        try:
            sheet_id = _get_sheet_id_by_title(sheets_api, sheet_title)
            if sheet_id is not None:
                try:
                    _ensure_date_column_format(sheets_api, sheet_id)
                except Exception:
                    logger.exception('Failed to ensure date column format')

                try:
                    _ensure_category_validation(sheets_api, sheet_id)
                except Exception:
                    logger.exception('Failed to ensure category validation')

                try:
                    _ensure_monthly_charts(sheets_api, sheet_title)
                except Exception:
                    logger.exception('Failed to ensure chart')
        except Exception:
            logger.exception('Failed to post-process sheet formatting/validation')

        return True
    except Exception as e:
        logger.exception('append_receipt_row failed: %s', e)
        return False


def _ensure_monthly_charts(sheets_api, sheet_title: str):
    """Create a category pie chart for the month and update/create a yearly monthly totals line chart.

    Implementation (best-effort):
    - Read rows from the monthly sheet, aggregate totals by category.
    - Write category summary into a '{sheet_title} - summary' sheet.
    - Add a PIE chart based on that summary (new sheet with chart).
    - Find all monthly sheets for the same year and aggregate monthly totals, write to a yearly summary sheet,
      and add a LINE chart for month vs total (new sheet with chart).
    """
    if not GOOGLE_SPREADSHEET_ID:
        return

    try:
        ss = sheets_api.spreadsheets().get(spreadsheetId=GOOGLE_SPREADSHEET_ID, includeGridData=False).execute()
        # find sheet object for this title
        sheet_meta = None
        for s in ss.get('sheets', []):
            props = s.get('properties', {})
            if props.get('title') == sheet_title:
                sheet_meta = props
                break
        if sheet_meta is None:
            return
        sheet_id = sheet_meta.get('sheetId')

        # Read the month sheet rows (A: date, B: store, C: total_amount, D: category)
        range_name = f"'{sheet_title}'!A2:D1000"
        resp = sheets_api.spreadsheets().values().get(spreadsheetId=GOOGLE_SPREADSHEET_ID, range=range_name).execute()
        rows = resp.get('values', [])

        # Aggregate by category
        cat_totals = {}
        month_total = 0.0
        for r in rows:
            # r may be shorter; ensure indexes
            total = _parse_amount(r[2]) if len(r) > 2 else 0.0
            cat = r[3].strip() if len(r) > 3 and r[3] else '未分類'
            cat_totals[cat] = cat_totals.get(cat, 0.0) + total
            month_total += total

        # Prepare summary sheet name and ensure it exists
        summary_title = f"{sheet_title} - summary"
        summary_id = _get_sheet_id_by_title(sheets_api, summary_title)
        requests = []
        if summary_id is None:
            requests.append({'addSheet': {'properties': {'title': summary_title}}})

        if requests:
            sheets_api.spreadsheets().batchUpdate(spreadsheetId=GOOGLE_SPREADSHEET_ID, body={'requests': requests}).execute()
            summary_id = _get_sheet_id_by_title(sheets_api, summary_title)

        # Write category totals to summary sheet
        summary_rows = [['category', 'total']]
        for k, v in sorted(cat_totals.items(), key=lambda x: -x[1]):
            summary_rows.append([k, v])
        sheets_api.spreadsheets().values().update(spreadsheetId=GOOGLE_SPREADSHEET_ID, range=f"'{summary_title}'!A1:B{len(summary_rows)}", valueInputOption='RAW', body={'values': summary_rows}).execute()

        # Create or replace a dedicated chart sheet for the PIE chart
        pie_chart_sheet_title = f"{sheet_title} - Chart"
        chart_sheet_id = _recreate_sheet(sheets_api, pie_chart_sheet_title)

        pie_requests = [
            {
                'addChart': {
                    'chart': {
                        'spec': {
                            'title': f'カテゴリー別支出 ({sheet_title})',
                            'pieChart': {
                                'legendPosition': 'RIGHT_LEGEND',
                                'threeDimensional': False,
                                'domain': {
                                    'sourceRange': {
                                        'sources': [{'sheetId': summary_id, 'startRowIndex': 1, 'startColumnIndex': 0, 'endColumnIndex': 1}]
                                    }
                                },
                                'series': {
                                    'sourceRange': {
                                        'sources': [{'sheetId': summary_id, 'startRowIndex': 1, 'startColumnIndex': 1, 'endColumnIndex': 2}]
                                    }
                                }
                            }
                        },
                        'position': {
                            'overlayPosition': {
                                'anchorCell': {'sheetId': chart_sheet_id, 'rowIndex': 0, 'columnIndex': 0}
                            }
                        }
                    }
                }
            }
        ]
        sheets_api.spreadsheets().batchUpdate(spreadsheetId=GOOGLE_SPREADSHEET_ID, body={'requests': pie_requests}).execute()

        # YEARLY monthly totals: determine year from sheet_title (assumes '... - YYYY-MM')
        try:
            year_part = sheet_title.split('-')[-1].strip()
            year = int(year_part.split('-')[0]) if '-' in year_part else int(year_part[:4])
        except Exception:
            # fallback: current year
            year = datetime.utcnow().year

        # collect monthly totals across sheets in this year
        monthly = {}
        for s in ss.get('sheets', []):
            t = s.get('properties', {}).get('title', '')
            if t.startswith(f"{GOOGLE_SHEET_NAME} - {year}-"):
                # read totals from column C
                r = sheets_api.spreadsheets().values().get(spreadsheetId=GOOGLE_SPREADSHEET_ID, range=f"'{t}'!C2:C1000").execute()
                vals = r.get('values', [])
                total = 0.0
                for v in vals:
                    if v:
                        total += _parse_amount(v[0])
                # extract month from title
                try:
                    m = int(t.rsplit('-', 1)[-1])
                except Exception:
                    m = None
                if m:
                    monthly[m] = total

        # write yearly summary sheet
        year_title = f"{GOOGLE_SHEET_NAME} - {year} Monthly Totals"
        year_id = _get_sheet_id_by_title(sheets_api, year_title)
        reqs = []
        if year_id is None:
            reqs.append({'addSheet': {'properties': {'title': year_title}}})
        if reqs:
            sheets_api.spreadsheets().batchUpdate(spreadsheetId=GOOGLE_SPREADSHEET_ID, body={'requests': reqs}).execute()
            year_id = _get_sheet_id_by_title(sheets_api, year_title)

        # build rows for months 1..12
        year_rows = [['month', 'total']]
        for m in range(1, 13):
            year_rows.append([m, monthly.get(m, 0.0)])
        sheets_api.spreadsheets().values().update(spreadsheetId=GOOGLE_SPREADSHEET_ID, range=f"'{year_title}'!A1:B13", valueInputOption='RAW', body={'values': year_rows}).execute()

        # create/replace a dedicated chart sheet for the yearly line chart
        year_chart_sheet_title = f"{year_title} - Chart"
        year_chart_sheet_id = _recreate_sheet(sheets_api, year_chart_sheet_title)

        line_requests = [
            {
                'addChart': {
                    'chart': {
                        'spec': {
                            'title': f'{year} 月別支出',
                            'basicChart': {
                                'chartType': 'LINE',
                                'legendPosition': 'BOTTOM_LEGEND',
                                'axis': [
                                    {'position': 'BOTTOM_AXIS', 'title': 'Month'},
                                    {'position': 'LEFT_AXIS', 'title': 'Amount'}
                                ],
                                'domains': [
                                    {
                                        'domain': {
                                            'sourceRange': {
                                                'sources': [{'sheetId': year_id, 'startRowIndex': 1, 'startColumnIndex': 0, 'endColumnIndex': 1}]
                                            }
                                        }
                                    }
                                ],
                                'series': [
                                    {
                                        'series': {
                                            'sourceRange': {
                                                'sources': [{'sheetId': year_id, 'startRowIndex': 1, 'startColumnIndex': 1, 'endColumnIndex': 2}]
                                            }
                                        },
                                        'targetAxis': 'LEFT_AXIS'
                                    }
                                ]
                            }
                        },
                        'position': {
                            'overlayPosition': {
                                'anchorCell': {'sheetId': year_chart_sheet_id, 'rowIndex': 0, 'columnIndex': 0}
                            }
                        }
                    }
                }
            }
        ]
        sheets_api.spreadsheets().batchUpdate(spreadsheetId=GOOGLE_SPREADSHEET_ID, body={'requests': line_requests}).execute()

    except Exception as e:
        logger.exception('Failed to ensure monthly charts: %s', e)


def _parse_amount(v: Any) -> float:
    try:
        if v is None:
            return 0.0
        s = str(v).strip()
        # remove common non-numeric characters
        s = s.replace(',', '').replace('¥', '').replace('$', '')
        # if contains spaces, take first token
        s = s.split()[0]
        return float(s)
    except Exception:
        return 0.0


def _recreate_sheet(sheets_api, title: str) -> int:
    """Delete existing sheet with `title` (if any) and create a new empty sheet with that title.
    Returns the new sheetId.
    """
    try:
        ss = sheets_api.spreadsheets().get(spreadsheetId=GOOGLE_SPREADSHEET_ID, includeGridData=False).execute()
        existing_id = None
        for s in ss.get('sheets', []):
            props = s.get('properties', {})
            if props.get('title') == title:
                existing_id = props.get('sheetId')
                break

        requests = []
        if existing_id is not None:
            requests.append({'deleteSheet': {'sheetId': existing_id}})

        requests.append({'addSheet': {'properties': {'title': title}}})

        body = {'requests': requests}
        resp = sheets_api.spreadsheets().batchUpdate(spreadsheetId=GOOGLE_SPREADSHEET_ID, body=body).execute()

        # after creation, fetch id
        return _get_sheet_id_by_title(sheets_api, title)
    except Exception:
        logger.exception('Failed to recreate sheet: %s', title)
        return None


def _sheet_title_for_data_date(raw_date: Any) -> str:
    """Return a sheet title string based on the provided date-like value.
    Falls back to the base `GOOGLE_SHEET_NAME` if parsing fails.
    """
    try:
        if isinstance(raw_date, (date, datetime)):
            dt = raw_date
        elif isinstance(raw_date, str):
            s = raw_date.strip()
            dt = None
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%Y.%m.%d"):
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except Exception:
                    dt = None
            if dt is None:
                try:
                    parts = s.split('-')
                    if len(parts) >= 2 and len(parts[0]) == 4:
                        year = int(parts[0]); month = int(parts[1])
                        dt = datetime(year, month, 1)
                    else:
                        dt = datetime.utcnow()
                except Exception:
                    dt = datetime.utcnow()
        else:
            dt = datetime.utcnow()

        year = dt.year
        month = dt.month
        return f"{GOOGLE_SHEET_NAME} - {year}-{month:02d}"
    except Exception:
        return GOOGLE_SHEET_NAME


def refresh_all_charts():
    """Public helper: refresh charts for all monthly sheets.

    Call this after manual edits to update summaries and charts.
    """
    gs, sheets_api = _build_clients()
    if not gs:
        return False
    try:
        ss = sheets_api.spreadsheets().get(spreadsheetId=GOOGLE_SPREADSHEET_ID, includeGridData=False).execute()
        for s in ss.get('sheets', []):
            title = s.get('properties', {}).get('title', '')
            # match monthly sheet name format
            if title.startswith(f"{GOOGLE_SHEET_NAME} - "):
                # expects YYYY-MM after suffix
                tail = title.split(f"{GOOGLE_SHEET_NAME} - ")[-1]
                if len(tail) >= 7 and tail[4] == '-':
                    try:
                        _ensure_monthly_charts(sheets_api, title)
                    except Exception:
                        logger.exception('Failed to refresh charts for %s', title)
        return True
    except Exception:
        logger.exception('Failed to refresh_all_charts')
        return False


def _get_sheet_id_by_title(sheets_api, title: str) -> Optional[int]:
    try:
        ss = sheets_api.spreadsheets().get(spreadsheetId=GOOGLE_SPREADSHEET_ID, includeGridData=False).execute()
        for s in ss.get('sheets', []):
            props = s.get('properties', {})
            if props.get('title') == title:
                return props.get('sheetId')
    except Exception:
        logger.exception('Failed to get sheet id for title: %s', title)
    return None


def _ensure_date_column_format(sheets_api, sheet_id: int):
    # set column A (index 0) to DATE format
    requests = [
        {
            'repeatCell': {
                'range': {
                    'sheetId': sheet_id,
                    'startRowIndex': 0,
                    'startColumnIndex': 0,
                    'endColumnIndex': 1
                },
                'cell': {
                    'userEnteredFormat': {
                        'numberFormat': {'type': 'DATE', 'pattern': 'yyyy-mm-dd'}
                    }
                },
                'fields': 'userEnteredFormat.numberFormat'
            }
        }
    ]
    body = {'requests': requests}
    sheets_api.spreadsheets().batchUpdate(spreadsheetId=GOOGLE_SPREADSHEET_ID, body=body).execute()


def _ensure_category_validation(sheets_api, sheet_id: int):
    """Add a ONE_OF_LIST validation on the category column (column D, index 3).
    Category options can be overridden by env var `GOOGLE_CATEGORY_OPTIONS` as comma-separated values or JSON list.
    """
    default = ['食費', '外食', '日用品(消耗品)', '日用品(非消耗品)', '交通費', '趣味', '光熱費', 'その他']
    opts_raw = os.environ.get('GOOGLE_CATEGORY_OPTIONS')
    if opts_raw:
        try:
            if opts_raw.strip().startswith('['):
                options = json.loads(opts_raw)
            else:
                options = [s.strip() for s in opts_raw.split(',') if s.strip()]
        except Exception:
            options = default
    else:
        options = default

    values = [{'userEnteredValue': v} for v in options]

    requests = [
        {
            'setDataValidation': {
                'range': {
                    'sheetId': sheet_id,
                    'startRowIndex': 1,
                    'startColumnIndex': 3,
                    'endColumnIndex': 4
                },
                'rule': {
                    'condition': {
                        'type': 'ONE_OF_LIST',
                        'values': values
                    },
                    'showCustomUi': True,
                    'strict': False
                }
            }
        }
    ]

    body = {'requests': requests}
    sheets_api.spreadsheets().batchUpdate(spreadsheetId=GOOGLE_SPREADSHEET_ID, body=body).execute()
