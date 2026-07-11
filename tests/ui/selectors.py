"""
Central selector constants for UI tests.

Every CSS selector used in UI tests MUST be defined here. When ui.html
changes an ID or class, update this single file - not every test.

Selectors are verified against the actual ui.html source and annotated
with their line numbers for traceability.
"""

# ── Navigation ────────────────────────────────────────────────────────────────

NAV_TAB = 'button.nav-tab[data-page="{page_id}"]'  # .format(page_id=...)
PAGE_CONTAINER = "#{page_id}"                        # .format(page_id=...)

PAGES = {
    "query":             "page-query",              # line 1641
    "lookups":           "page-lookups",             # line 1919
    "import":            "page-import",              # line 1956
    "create_search":     "page-create-search",       # line 2009
    "searches":          "page-searches",             # line 2112
    "create_ingestion":  "page-create-ingestion",    # line 2126
    "ingestion":         "page-ingestion",            # line 2258
    "library":           "page-library",              # line 2287
    "docs":              "page-docs",                 # line 2527
    "settings":          "page-settings",             # line 2316
    "macros":            "page-macros",               # line 2590
    "alert_groups":      "page-alert-groups",          # line 3115
}

# ── Alert Groups Page ─────────────────────────────────────────────────────────

AG_NEW_BTN = "#ag-new-btn"
AG_REFRESH_BTN = "#ag-refresh-btn"
AG_LIST = "#ag-list"
AG_MESSAGE = "#ag-message"
AG_FORM_BOX = "#ag-form-box"
AG_FORM_TITLE = "#ag-form-title"
AG_NAME_INPUT = "#ag-name"
AG_DESCRIPTION_INPUT = "#ag-description"

# ── Themes ────────────────────────────────────────────────────────────────────

THEME_BTN = 'button.theme-btn[data-theme="{theme}"]'   # line 1616-1619
HTML_ELEMENT = "html"

# ── Notifications ─────────────────────────────────────────────────────────────

NOTIFICATION_CONTAINER = "#notification-container"      # line 2692
NOTIFICATION = "#notification-container .notification"
NOTIFICATION_SUCCESS = "#notification-container .notification.is-success"
NOTIFICATION_ERROR = "#notification-container .notification.is-danger"
NOTIFICATION_PRIMARY = "#notification-container .notification.is-primary"

# ── First-Run Overlays ────────────────────────────────────────────────────────

WELCOME_BACKDROP = "#welcome-backdrop"                  # line 1684
WELCOME_PANEL = "#welcome-panel"                        # line 1685
WELCOME_GO_BTN = "#welcome-go-btn"                      # line 1792
WELCOME_DISMISS_CHECK = "#welcome-dismiss-check"        # line 1789
EMAIL_SETUP_BACKDROP = "#email-setup-backdrop"          # line 1644
EMAIL_SETUP_PANEL = "#email-setup-panel"                # line 1645

# ── Query Page ────────────────────────────────────────────────────────────────

QUERY_INPUT = "#query"                                  # line 1805
RUN_QUERY_BTN = "#run-query-btn"                        # line 1811
SAVE_CSV_BTN = "#save-csv-btn"                          # line 1812
SAVE_JSON_BTN = "#save-json-btn"                        # line 1813
SAVE_JOB_BTN = "#save-job-btn"                          # line 1814
EXPAND_MACROS_BTN = "#expand-macros-btn"                # line 1818
EXPAND_MACROS_DEPTH = "#expand-macros-depth"            # line 1820
SAVE_JOB_PANEL = "#save-job-panel"                      # line 1827
SAVE_JOB_NAME = "#save-job-name"                        # line 1831
SAVE_JOB_TTL = "#save-job-ttl"                          # line 1836
SAVE_JOB_CONFIRM_BTN = "#save-job-confirm-btn"          # line 1843
SAVE_JOB_CANCEL_BTN = "#save-job-cancel-btn"            # line 1844
JOB_ID_BAR = "#job-id-bar"                              # line 1849
JOB_ID_LABEL = "#job-id-label"                          # line 1850
SPINNER = "#spinner"                                    # line 1853
ROW_COUNT = "#row-count"                                # line 1858
RESULTS = "#results"                                    # line 1868
TREE_SEARCH_INPUT = "#tree-search-input"                # line 1888
DIRECTORY_TREE = "#directory-tree"                       # line 1895
DIRECTORY_TREE_MESSAGE = "#directory-tree-message"       # line 1896

# Pagination (top)
PAGINATION_TOP = "#pagination-top"
PREV_TOP = "#prev-top"
NEXT_TOP = "#next-top"
PAGE_INFO_TOP = "#page-info-top"

# ── Settings Page ─────────────────────────────────────────────────────────────

SETTINGS_MESSAGE = "#settings-message"                  # line 2520
SAVE_SETTINGS_BTN = "#save-settings-btn"                # line 2517
RESET_SETTINGS_BTN = "#reset-settings-btn"              # line 2518
TEST_EMAIL_BTN = "#test-email-btn"                      # line 2510
TEST_EMAIL_MSG = "#test-email-msg"                      # line 2511

# Settings fields
SET_INDEXES_ROOT = "#set-indexes-root"
SET_MAX_TOTAL_SIZE_GB = "#set-max-total-size-gb"
SET_MAX_SUBDIRECTORY_SIZE_GB = "#set-max-subdirectory-size-gb"
SET_MAX_PARQUET_FILE_MB = "#set-max-parquet-file-mb"
SET_CLEANUP_INTERVAL_HOURS = "#set-cleanup-interval-hours"
SET_DEFAULT_SCRIPT_TIMEOUT = "#set-default-script-timeout-seconds"
SET_MAX_RETRIES = "#set-max-retries"
SET_HTTP_REQUEST_TIMEOUT = "#set-http-request-timeout-seconds"
SET_MAX_SUBDIRECTORY_DEPTH = "#set-max-subdirectory-depth"
SET_MAX_OUTPUT_ROWS = "#set-max-output-rows"
SET_MAX_REQUESTS_PER_EXECUTION = "#set-max-requests-per-execution"
SET_MAX_RESPONSE_SIZE_MB = "#set-max-response-size-mb"
SET_CREDENTIAL_KEY_DIR = "#set-credential-key-dir"
SET_ALLOWED_API_DOMAINS = "#set-allowed-api-domains"
SET_SMTP_SERVER = "#set-smtp-server"
SET_SMTP_PORT = "#set-smtp-port"
SET_SMTP_USER = "#set-smtp-user"
SET_SMTP_PASSWORD = "#set-smtp-password"
SET_SMTP_FROM = "#set-smtp-from"
SET_SMTP_STARTTLS = "#set-smtp-starttls"

# ── Lookups Page ──────────────────────────────────────────────────────────────

LOOKUPS_LIST = "#lookups-list"
LOOKUPS_MESSAGE = "#lookups-message"
LOOKUPS_PREVIEW_VIEW = "#lookups-preview-view"
LOOKUPS_LIST_VIEW = "#lookups-list-view"
LOOKUP_PREVIEW_TITLE = "#lookup-preview-title"
LOOKUP_PREVIEW = "#lookup-preview"
LOOKUP_BACK_BTN = "#lookup-back-btn"
UPLOAD_LOOKUP_BTN = "#upload-lookup-btn"
UPLOAD_LOOKUP_INPUT = "#upload-lookup-input"
REFRESH_LOOKUPS_BTN = "#refresh-lookups-btn"

# ── Import Page ───────────────────────────────────────────────────────────────

IMPORT_FILE_BTN = "#import-file-btn"
IMPORT_FILE_INPUT = "#import-file-input"
IMPORT_FILE_LABEL = "#import-file-label"
IMPORT_INDEX_NAME = "#import-index-name"
IMPORT_DATE_FIELD = "#import-date-field"
IMPORT_SQLITE_TABLE = "#import-sqlite-table"
IMPORT_SQLITE_SECTION = "#import-sqlite-section"
IMPORT_SUBMIT_BTN = "#import-submit-btn"
IMPORT_STATUS = "#import-status"

# ── Searches Page ─────────────────────────────────────────────────────────────

SEARCHES_LIST = "#searches-list"
SEARCHES_MESSAGE = "#searches-message"
REFRESH_SEARCHES_BTN = "#refresh-searches-btn"
YAML_VIEWER_MODAL = "#yaml-viewer-modal"
YAML_MODAL_BACKDROP = "#yaml-modal-backdrop"
YAML_VIEWER_TITLE = "#yaml-viewer-title"
YAML_VIEWER_CONTENT = "#yaml-viewer-content"

# ── Create Search Page ────────────────────────────────────────────────────────

SS_NAME = "#ss-name"
SS_QUERY = "#ss-query"
SS_DESCRIPTION = "#ss-description"
SS_CRON = "#ss-cron"
SS_LOOKBACK = "#ss-lookback"
SS_TRIGGER = "#ss-trigger"
SS_EMAIL = "#ss-email"
SS_SAVE_BTN = "#ss-save-btn"
SS_CLEAR_BTN = "#ss-clear-btn"

# ── Macros Page ───────────────────────────────────────────────────────────────

MACROS_LIST = "#macros-list"
MACROS_MESSAGE = "#macros-message"
MACRO_FORM_BOX = "#macro-form-box"
MACRO_NAME = "#macro-name"
MACRO_DEFINITION = "#macro-definition"
MACRO_PARAMETERS = "#macro-parameters"
MACRO_DESCRIPTION = "#macro-description"
MACRO_SAVE_BTN = "#macro-save-btn"
MACRO_CANCEL_BTN = "#macro-cancel-btn"
MACRO_NEW_BTN = "#macro-new-btn"
MACRO_REFRESH_BTN = "#macro-refresh-btn"
MACRO_TEST_QUERY = "#macro-test-query"
MACRO_EXPAND_BTN = "#macro-expand-btn"
MACRO_TEST_BTN = "#macro-test-btn"

# ── Ingestion Pages ───────────────────────────────────────────────────────────

SI_TITLE = "#si-title"
SI_DESCRIPTION = "#si-description"
SI_CRON = "#si-cron"
SI_SUBDIRECTORY = "#si-subdirectory"
SI_OVERWRITE = "#si-overwrite"
SI_CODE = "#si-code"
SI_TEST_BTN = "#si-test-btn"
SI_SAVE_BTN = "#si-save-btn"
SI_CLEAR_BTN = "#si-clear-btn"
SI_SCRIPTS_LIST = "#si-scripts-list"
SI_SCRIPTS_MESSAGE = "#si-scripts-message"
SI_NEW_BTN = "#si-new-btn"
REFRESH_SI_BTN = "#refresh-si-btn"

# ── Library Page ──────────────────────────────────────────────────────────────

LIB_GRID = "#lib-grid"
LIB_MESSAGE = "#lib-message"
LIB_MODAL = "#lib-modal"
LIB_MODAL_BACKDROP = "#lib-modal-backdrop"
LIB_MODAL_TITLE = "#lib-modal-title"
LIB_MODAL_BODY = "#lib-modal-body"
LIB_MODAL_CLOSE = "#lib-modal-close"
REFRESH_LIB_BTN = "#refresh-lib-btn"

# ── Docs Page ─────────────────────────────────────────────────────────────────

DOCS_NAV = "#docs-nav"
DOCS_CONTENT = "#docs-content"
DOCS_SEARCH = "#docs-search"

# ── History Modal ─────────────────────────────────────────────────────────────

HISTORY_MODAL = "#history-modal"
HISTORY_MODAL_BACKDROP = "#history-modal-backdrop"
HISTORY_MODAL_TITLE = "#history-modal-title"
HISTORY_MODAL_BODY = "#history-modal-body"
HISTORY_MODAL_CLOSE = "#history-modal-close"
