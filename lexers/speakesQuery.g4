grammar speakesQuery;

speakesQuery
    : (NEWLINE | WS)* initialSequence (NEWLINE | WS)* validLine* (EOF | NEWLINE?)
    ;

initialSequence
    : expression ((NEWLINE | WS)* expression)*
    | inputlookupInit
    | loadjobInit
    | makeresultsInit
    ;

expression
    : conjunction ((NEWLINE | WS)* OR (NEWLINE | WS)* conjunction)*
    ;

conjunction
    : comparison ( ((NEWLINE | WS)* AND (NEWLINE | WS)* | (NEWLINE | WS)*) comparison )*
    ;

comparison
    : additiveExpr ((NEWLINE | WS)* comparisonOperator (NEWLINE | WS)* additiveExpr)*
    ;

additiveExpr
    : multiplicativeExpr ( (PLUS | MINUS) multiplicativeExpr)*
    ;

multiplicativeExpr
    : unaryExpr ( (MUL | DIV) unaryExpr)*
    ;

unaryExpr
    : (NOT | PLUS | MINUS)? (NEWLINE | WS)* primary
    ;

primary
    : LPAREN (NEWLINE | WS)* expression (NEWLINE | WS)* RPAREN
    | timeClause
    | indexClause
    | inExpression
    | functionCall
    | variableName
    | DOUBLE_QUOTED_STRING
    | NUMBER
    | BOOLEAN
    ;

timeClause
    : earliestClause latestClause?
    | latestClause earliestClause?
    ;

earliestClause
    : EARLIEST EQUALS (DOUBLE_QUOTED_STRING | NUMBER | TIMESPEC) (NEWLINE | WS)*
    ;

latestClause
    : LATEST EQUALS (DOUBLE_QUOTED_STRING | NUMBER | TIMESPEC) (NEWLINE | WS)*
    ;

indexClause
    : INDEX EQUALS (DOUBLE_QUOTED_STRING | NUMBER | VARIABLE) (NEWLINE | WS)*
    ;

comparisonOperator
    : EQUALS | NOT_EQUALS | GT | LT | GTEQ | LTEQ
    ;

inExpression
    : (NOT? (NEWLINE | WS)* | (NEWLINE | WS)* NOT?) variableName IN LPAREN (expression (COMMA expression)*)? RPAREN
    ;

inputlookupInit
    : (NEWLINE | WS)* PIPE INPUTLOOKUP (variableName | DOUBLE_QUOTED_STRING) (NEWLINE | WS)*
    ;

loadjobInit
    : (NEWLINE | WS)* PIPE LOADJOB (variableName | DOUBLE_QUOTED_STRING) (NEWLINE | WS)*
    ;

makeresultsInit
    : (NEWLINE | WS)* PIPE MAKERESULTS (makeresultsArg)* (NEWLINE | WS)*
    ;

makeresultsArg
    : (NEWLINE | WS)* (variableName | COUNT) EQUALS (NUMBER | BOOLEAN) (NEWLINE | WS)*
    ;

validLine
    : (NEWLINE | WS)* PIPE directive (NEWLINE | WS)*
    ;

directive
    : SEARCH (NOT? (NEWLINE | WS)* | (NEWLINE | WS)* NOT?) (expression COMMA?)+
    | WHERE (NOT? (NEWLINE | WS)* | (NEWLINE | WS)* NOT?) (expression COMMA?)+
    | EVAL (DOUBLE_QUOTED_STRING | variableName) EQUALS expression (COMMA (NEWLINE | WS)* (DOUBLE_QUOTED_STRING | variableName) EQUALS expression)*
    | TABLE variableName (COMMA? variableName)*
    | MAKETABLE variableName (COMMA variableName)*
    | EVENTSTATS (NEWLINE | WS)* (statsAgg (COMMA? NEWLINE? statsAgg)*)? ((NEWLINE | WS)* COUNT (AS variableName)?)? (BY variableList)?
    | STATS (NEWLINE | WS)* statsAgg (COMMA? (NEWLINE | WS)* statsAgg)* ((NEWLINE | WS)* BY variableList)?
    | STREAMSTATS (NEWLINE | WS)* statsAgg (COMMA? (NEWLINE | WS)* statsAgg)* ((NEWLINE | WS)* BY variableList)?
    | TIMECHART (NEWLINE | WS)* (SPAN EQUALS timespan)? statsAgg (COMMA? (NEWLINE | WS)* statsAgg)* ((NEWLINE | WS)* BY variableList)?
    | RENAME (NEWLINE | WS)* (variableName AS (variableName | DOUBLE_QUOTED_STRING) COMMA? (NEWLINE | WS)*)+
    | FIELDS (PLUS | MINUS)? variableName (COMMA? variableName)*
    | MVEXPAND variableName
    | LOOKUP (VARIABLE | DOUBLE_QUOTED_STRING)
    | (HEAD | LIMIT) NUMBER
    | BIN (variableName SPAN EQUALS timespan | SPAN EQUALS timespan variableName)
    | REVERSE
    | DEDUP NUMBER? (CONSECUTIVE EQUALS BOOLEAN)? variableName (COMMA? variableName)*
    | NEAREST DOUBLE_QUOTED_STRING (TOPK EQUALS NUMBER)? (THRESHOLD EQUALS NUMBER)? (FIELD EQUALS variableName)?
    | DEDUP_SEMANTIC (THRESHOLD EQUALS NUMBER)? (FIELD EQUALS variableName)?
    | LLM MODEL EQUALS DOUBLE_QUOTED_STRING PROMPT EQUALS DOUBLE_QUOTED_STRING (SYSTEM EQUALS DOUBLE_QUOTED_STRING)? (FIELD EQUALS variableName)? (USE_CACHE EQUALS BOOLEAN)? (MAX_TOKENS EQUALS NUMBER)? (MAX_COST_USD EQUALS NUMBER)? (DRY_RUN EQUALS BOOLEAN)?
    | LLM_BATCH MODEL EQUALS DOUBLE_QUOTED_STRING PROMPT EQUALS DOUBLE_QUOTED_STRING (SYSTEM EQUALS DOUBLE_QUOTED_STRING)? (FIELD EQUALS variableName)? (USE_CACHE EQUALS BOOLEAN)? (MAX_TOKENS EQUALS NUMBER)? (MAX_ROWS EQUALS NUMBER)? (MAX_COST_USD EQUALS NUMBER)? (DRY_RUN EQUALS BOOLEAN)?
    | LLM_ROUTE MODEL EQUALS DOUBLE_QUOTED_STRING PROMPT EQUALS DOUBLE_QUOTED_STRING ESCALATE_TO EQUALS DOUBLE_QUOTED_STRING (ESCALATE_PROMPT EQUALS DOUBLE_QUOTED_STRING)? (CONFIDENCE_THRESHOLD EQUALS NUMBER)? (SYSTEM EQUALS DOUBLE_QUOTED_STRING)? (FIELD EQUALS variableName)? (USE_CACHE EQUALS BOOLEAN)? (MAX_TOKENS EQUALS NUMBER)? (MAX_COST_USD EQUALS NUMBER)? (DRY_RUN EQUALS BOOLEAN)?
    | LLM_REFINE DRAFTER_MODEL EQUALS DOUBLE_QUOTED_STRING CRITIC_MODEL EQUALS DOUBLE_QUOTED_STRING DRAFTER_PROMPT EQUALS DOUBLE_QUOTED_STRING CRITIC_PROMPT EQUALS DOUBLE_QUOTED_STRING (REVISE_PROMPT EQUALS DOUBLE_QUOTED_STRING)? (MAX_ROUNDS EQUALS NUMBER)? (CONVERGE_WHEN_CRITIC_SAYS EQUALS DOUBLE_QUOTED_STRING)? (SYSTEM EQUALS DOUBLE_QUOTED_STRING)? (FIELD EQUALS variableName)? (USE_CACHE EQUALS BOOLEAN)? (MAX_TOKENS EQUALS NUMBER)? (MAX_COST_USD EQUALS NUMBER)? (DRY_RUN EQUALS BOOLEAN)?
    | LLM_ENSEMBLE MODELS EQUALS DOUBLE_QUOTED_STRING PROMPT EQUALS DOUBLE_QUOTED_STRING (AGGREGATOR EQUALS DOUBLE_QUOTED_STRING)? (MIN_AGREEMENT EQUALS NUMBER)? (SYSTEM EQUALS DOUBLE_QUOTED_STRING)? (FIELD EQUALS variableName)? (USE_CACHE EQUALS BOOLEAN)? (MAX_TOKENS EQUALS NUMBER)? (MAX_COST_USD EQUALS NUMBER)? (DRY_RUN EQUALS BOOLEAN)?
    | LLM_UNTIL MODEL EQUALS DOUBLE_QUOTED_STRING PROMPT EQUALS DOUBLE_QUOTED_STRING MAX_ITERATIONS EQUALS NUMBER (ITERATE_PROMPT EQUALS DOUBLE_QUOTED_STRING)? (CONVERGE_WHEN_OUTPUT_CONTAINS EQUALS DOUBLE_QUOTED_STRING)? (CONVERGE_WHEN_OUTPUT_UNCHANGED EQUALS BOOLEAN)? (CONVERGE_WHEN_BELOW_CONFIDENCE EQUALS NUMBER)? (SYSTEM EQUALS DOUBLE_QUOTED_STRING)? (FIELD EQUALS variableName)? (USE_CACHE EQUALS BOOLEAN)? (MAX_TOKENS EQUALS NUMBER)? (MAX_COST_USD EQUALS NUMBER)? (DRY_RUN EQUALS BOOLEAN)?
    | SWITCH variableName (CASE DOUBLE_QUOTED_STRING subsearch)+
    | SORT (PLUS | MINUS) NUMBER? (variableName COMMA?)+
    | REX FIELD EQUALS variableName (MODE EQUALS SED)? (MAX_MATCH EQUALS NUMBER)? DOUBLE_QUOTED_STRING
    | REGEX variableName (EQUALS | NOT_EQUALS) (variableName | DOUBLE_QUOTED_STRING)
    | BASE64 (ENCODE | DECODE) (variableName | DOUBLE_QUOTED_STRING) (COMMA? (variableName | DOUBLE_QUOTED_STRING))*
    | BACKTICK macro BACKTICK
    | FILLNULL VALUE EQUALS (DOUBLE_QUOTED_STRING | NUMBER | variableName) (variableName COMMA?)*
    | SPATH variableName OUTPUT EQUALS variableName
    | JOIN (TYPE EQUALS (LEFT | CENTER | RIGHT))? variableName (COMMA variableName)* subsearch
    | APPEND subsearch
    | APPENDPIPE subsearch
    | MULTISEARCH subsearch+
    | COALESCE LPAREN variableName (COMMA variableName)+ RPAREN
    | MVJOIN LPAREN expression COMMA mvDelim RPAREN
    | MVINDEX LPAREN expression COMMA mvindexIndex RPAREN
    | MVREVERSE LPAREN expression RPAREN
    | MVFIND LPAREN expression COMMA mvfindObject RPAREN
    | MVDEDUP LPAREN expression RPAREN
    | MVAPPEND LPAREN expression (COMMA (variableName | expression))+ RPAREN
    | MVFILTER LPAREN expression RPAREN
    | MVCOMBINE LPAREN expression COMMA mvDelim RPAREN
    | MVCOUNT LPAREN expression RPAREN
    | MVDC LPAREN expression RPAREN
    | MVZIP LPAREN expression (COMMA variableName)+ COMMA mvDelim RPAREN
    | FIELDSUMMARY (LPAREN RPAREN)?
    | OUTPUTLOOKUP (variableName | DOUBLE_QUOTED_STRING) (WINDOW EQUALS NUMBER)? (OVERWRITE | OVERWRITE_IF_EMPTY | CREATE_EMPTY)*
    | OUTPUTNEW (variableName | DOUBLE_QUOTED_STRING)
    | MAKERESULTS (makeresultsArg)*
    | ADDINFO
    ;

macro
    : VARIABLE LPAREN (COMMA? (DOUBLE_QUOTED_STRING | VARIABLE))* RPAREN
    ;

statsAgg
    : statsFunctionCall (AS variableName)?
    ;

variableList
    : variableName (COMMA variableName)*
    ;

subsearch
    : LBRACK subsearchContent RBRACK
    ;

subsearchContent
    : ~RBRACK*
    ;

functionCall
    : numericFunctionCall
    | stringFunctionCall
    | specificFunctionCall
    ;

numericFunctionCall
    : ROUND LPAREN expression (COMMA expression)? RPAREN
    | MIN LPAREN expression (COMMA expression)* RPAREN
    | MAX LPAREN expression (COMMA expression)* RPAREN
    | AVG LPAREN expression (COMMA expression)* RPAREN
    // sum / range / median / mode used to be single-arg only - matched
    // the stats-command shape ``stats median(field)`` but rejected the
    // eval-context multi-arg shape ``eval r = median(a, b, c)``. The
    // runtime handler already supports both shapes (stats picks the
    // first arg as the column; eval treats every arg as a value to
    // include). Variadic grammar = consistent with min/max/avg and
    // unblocks docs-promised eval usage. Fixed 2026-05-16.
    | SUM LPAREN expression (COMMA expression)* RPAREN
    | RANGE LPAREN expression (COMMA expression)* RPAREN
    | MEDIAN LPAREN expression (COMMA expression)* RPAREN
    | MODE LPAREN expression (COMMA expression)* RPAREN
    | SQRT LPAREN expression RPAREN
    | ABS LPAREN expression RPAREN
    | FLOOR LPAREN expression RPAREN
    | CEIL LPAREN expression RPAREN
    | RANDOM LPAREN (expression (COMMA expression)*)? RPAREN
    | NOW LPAREN RPAREN
    | RANDOMIZE LPAREN expression RPAREN
    ;

stringFunctionCall
    : CONCAT LPAREN expression (COMMA expression)* RPAREN
    | REPLACE LPAREN expression COMMA expression COMMA expression RPAREN
    | UPPER LPAREN expression RPAREN
    | LOWER LPAREN expression RPAREN
    | CAPITALIZE LPAREN expression RPAREN
    | LEN LPAREN expression RPAREN
    | TOSTRING LPAREN expression RPAREN
    | URLENCODE LPAREN expression RPAREN
    | URLDECODE LPAREN expression RPAREN
    | DEFANG LPAREN expression RPAREN
    | FANG LPAREN expression RPAREN
    | TRIM LPAREN expression (COMMA expression)? RPAREN
    | RTRIM LPAREN expression (COMMA expression)? RPAREN
    | LTRIM LPAREN expression (COMMA expression)? RPAREN
    | SUBSTR LPAREN expression COMMA expression COMMA expression RPAREN
    | SPLIT LPAREN expression COMMA expression RPAREN
    | TYPE LPAREN expression RPAREN
    | BASE64_ENCODE LPAREN expression RPAREN
    | BASE64_DECODE LPAREN expression RPAREN
    | STRFTIME LPAREN expression COMMA expression RPAREN
    | STRPTIME LPAREN expression (COMMA expression)? RPAREN
    | RELATIVE_TIME LPAREN expression RPAREN
    | (NOT (NEWLINE | WS)*)? MATCH LPAREN (variableName | DOUBLE_QUOTED_STRING) COMMA regexTarget RPAREN
    ;

specificFunctionCall
    : (NOT (NEWLINE | WS)*)? ISNULL LPAREN variableName RPAREN
    | (NOT (NEWLINE | WS)*)? ISNOTNULL LPAREN variableName RPAREN
    | COALESCE LPAREN variableName (COMMA variableName)+ RPAREN
    | IF_ LPAREN expression COMMA expression COMMA expression RPAREN
    | CASE LPAREN expression (COMMA expression)+ RPAREN
    | TONUMBER LPAREN expression RPAREN
    | MVSORT LPAREN expression RPAREN
    ;

statsFunctionCall
    : COUNT
    | COUNT LPAREN expression RPAREN
    | VALUES LPAREN expression RPAREN
    | LATEST LPAREN expression RPAREN
    | EARLIEST LPAREN expression RPAREN
    | FIRST LPAREN expression RPAREN
    | LAST LPAREN expression RPAREN
    | DC LPAREN expression RPAREN
    // No-parens shorthand for aggregators (Splunk-idiomatic). The
    // StatsHandler accepts ``stats avg as A by X`` and computes
    // avg() over the implicit-numeric column; previously the grammar
    // rejected this and ANTLR error-recovered (functionally correct
    // but noisy). Added 2026-05-16 alongside the alias-drop regex
    // fix in StatsHandler._parse_function_specs.
    | DC
    | MIN
    | MAX
    | AVG
    | SUM
    | MEDIAN
    | MODE
    | RANGE
    | numericFunctionCall
    ;

regexTarget
    : variableName
    | DOUBLE_QUOTED_STRING
    ;

mvfindObject
    : variableName
    | DOUBLE_QUOTED_STRING
    | unaryExpr
    ;

mvindexIndex
    : unaryExpr
    ;

mvDelim
    : DOUBLE_QUOTED_STRING
    ;

timespan
    : NUMBER (SECONDS | MINUTES | HOURS | DAYS | WEEKS | YEARS)
    ;

variableName
    : VARIABLE
    | SINGLE_QUOTED_STRING
    | TYPE
    | VALUE
    | EARLIEST
    | LATEST
    | INDEX
    | NOW
    | SPLIT
    | RELATIVE_TIME
    | STRFTIME
    | STRPTIME
    | RANDOMIZE
    | BASE64_ENCODE
    | BASE64_DECODE
    | MVSORT
    // Stats output / aggregation token names that users commonly refer to
    // as columns downstream (e.g. ``stats count by x | sort -count``).
    | COUNT
    | VALUES
    | FIRST
    | LAST
    | DC
    | MIN
    | MAX
    | AVG
    | SUM
    | MEDIAN
    | RANGE
    ;

COMMENT                 : '#' ~[\r\n]* NEWLINE -> skip ;
WS                      : [ \t]+ -> channel(HIDDEN) ;
NEWLINE                 : '\r'? '\n' ;

PIPE                    : '|' ;

EARLIEST                : 'earliest' ;
LATEST                  : 'latest' ;
INDEX                   : 'index' ;
NOT                     : ('NOT' | 'not') ;
AND                     : ('AND' | 'and') ;
OR                      : ('OR' | 'or') ;
BY                      : ('BY' | 'by') ;
AS                      : ('AS' | 'as') ;
IN                      : ('IN' | 'in') ;
IF_                     : 'if_' ;
CASE                    : 'case' ;
TONUMBER                : 'tonumber' ;
EQUALS                  : '=' ;
NOT_EQUALS              : '!=' ;
GT                      : '>' ;
LT                      : '<' ;
GTEQ                    : '>=' ;
LTEQ                    : '<=' ;
PLUS                    : '+' ;
MINUS                   : '-' ;
MUL                     : '*' ;
DIV                     : '/' ;
LPAREN                  : '(' ;
RPAREN                  : ')' ;
LBRACK                  : '[' ;
RBRACK                  : ']' ;
COMMA                   : ',' ;
TABLE                   : 'table' ;
MAKETABLE               : 'maketable' ;
CONSECUTIVE             : 'consecutive' ;
MAX_MATCH               : ('max_match' | 'MAX_MATCH') ;
MVEXPAND                : 'mvexpand' ;
REVERSE                 : 'reverse' ;
MVREVERSE               : 'mvreverse' ;
MVCOMBINE               : 'mvcombine' ;
MVFIND                  : 'mvfind' ;
DEDUP                   : 'dedup' ;
DEDUP_SEMANTIC          : 'dedup_semantic' ;
NEAREST                 : 'nearest' ;
TOPK                    : 'topk' ;
THRESHOLD               : 'threshold' ;
LLM_BATCH               : 'llm_batch' ;
LLM_ROUTE               : 'llm_route' ;
LLM_REFINE              : 'llm_refine' ;
LLM_ENSEMBLE            : 'llm_ensemble' ;
LLM_UNTIL               : 'llm_until' ;
ESCALATE_TO             : 'escalate_to' ;
ESCALATE_PROMPT         : 'escalate_prompt' ;
CONFIDENCE_THRESHOLD    : 'confidence_threshold' ;
DRAFTER_MODEL           : 'drafter_model' ;
CRITIC_MODEL            : 'critic_model' ;
DRAFTER_PROMPT          : 'drafter_prompt' ;
CRITIC_PROMPT           : 'critic_prompt' ;
REVISE_PROMPT           : 'revise_prompt' ;
MAX_ROUNDS              : 'max_rounds' ;
CONVERGE_WHEN_CRITIC_SAYS : 'converge_when_critic_says' ;
AGGREGATOR              : 'aggregator' ;
MIN_AGREEMENT           : 'min_agreement' ;
ITERATE_PROMPT          : 'iterate_prompt' ;
MAX_ITERATIONS          : 'max_iterations' ;
CONVERGE_WHEN_OUTPUT_CONTAINS  : 'converge_when_output_contains' ;
CONVERGE_WHEN_OUTPUT_UNCHANGED : 'converge_when_output_unchanged' ;
CONVERGE_WHEN_BELOW_CONFIDENCE : 'converge_when_below_confidence' ;
LLM                     : 'llm' ;
// MODELS must come BEFORE MODEL for ANTLR longest-match precedence -
// otherwise `models="..."` would lex as MODEL "s=..." which fails.
MODELS                  : 'models' ;
MODEL                   : 'model' ;
PROMPT                  : 'prompt' ;
SYSTEM                  : 'system' ;
USE_CACHE               : 'use_cache' ;
MAX_TOKENS              : 'max_tokens' ;
MAX_ROWS                : 'max_rows' ;
MAX_COST_USD            : 'max_cost_usd' ;
DRY_RUN                 : 'dry_run' ;
SWITCH                  : 'switch' ;
MVDEDUP                 : 'mvdedup' ;
MVFILTER                : 'mvfilter' ;
COUNT                   : 'count' ;
MVCOUNT                 : 'mvcount' ;
DC                      : 'dc' ;
MVDC                    : 'mvdc' ;
MVZIP                   : 'mvzip' ;
FIRST                   : 'first' ;
LAST                    : 'last' ;
SORT                    : 'sort' ;
EVAL                    : 'eval' ;
SPAN                    : ('span' | 'SPAN') ;
BIN                     : 'bin' ;
STATS                   : 'stats' ;
EVENTSTATS              : 'eventstats' ;
STREAMSTATS             : 'streamstats' ;
TIMECHART               : 'timechart' ;
VALUE                   : ('value' | 'VALUE') ;
VALUES                  : 'values' ;
WHERE                   : ('WHERE' | 'where') ;
RENAME                  : 'rename' ;
FIELD                   : ('field' | 'FIELD') ;
FIELDS                  : 'fields' ;
FIELDSUMMARY            : 'fieldsummary' ;
APPEND                  : 'append' ;
APPENDPIPE              : 'appendpipe' ;
MVAPPEND                : 'mvappend' ;
SEARCH                  : 'search' ;
MULTISEARCH             : 'multisearch' ;
HEAD                    : 'head' ;
LIMIT                   : ('limit' | 'LIMIT') ;
REX                     : 'rex' ;
REGEX                   : 'regex' ;
LOADJOB                 : 'loadjob' ;
MAKERESULTS             : 'makeresults' ;
ADDINFO                 : 'addinfo' ;
OUTPUT                  : ('output' | 'OUTPUT') ;
LOOKUP                  : 'lookup' ;
INPUTLOOKUP             : 'inputlookup' ;
OUTPUTLOOKUP            : 'outputlookup' ;
OUTPUTNEW               : ('outputnew' | 'OUTPUTNEW') ;
WINDOW                  : ('window' | 'WINDOW') ;
OVERWRITE               : ('overwrite' | 'OVERWRITE') ;
OVERWRITE_IF_EMPTY      : ('overwrite_if_empty' | 'OVERWRITE_IF_EMPTY') ;
CREATE_EMPTY            : ('create_empty' | 'CREATE_EMPTY') ;
FILLNULL                : 'fillnull' ;
FANG                    : 'fang' ;
DEFANG                  : 'defang' ;
ROUND                   : 'round' ;
MIN                     : 'min' ;
MAX                     : 'max' ;
MEDIAN                  : 'median' ;
MODE                    : 'mode' ;
AVG                     : 'avg' ;
SUM                     : 'sum' ;
RANDOM                  : 'random' ;
SQRT                    : 'sqrt' ;
RANGE                   : 'range' ;
FLOOR                   : 'floor' ;
CEIL                    : 'ceil' ;
COALESCE                : 'coalesce' ;
ISNULL                  : 'isnull' ;
ISNOTNULL               : 'isnotnull' ;
LEN                     : 'len' ;
SED                     : 'sed' ;
CONCAT                  : 'concat' ;
REPLACE                 : 'replace' ;
LOWER                   : 'lower' ;
UPPER                   : 'upper' ;
CAPITALIZE              : 'capitalize' ;
TRIM                    : 'trim' ;
LTRIM                   : 'ltrim' ;
RTRIM                   : 'rtrim' ;
MATCH                   : 'match' ;
MVINDEX                 : 'mvindex' ;
JOIN                    : 'join' ;
MVJOIN                  : 'mvjoin' ;
SUBSTR                  : 'substr' ;
TOSTRING                : 'tostring' ;
TYPE                    : 'type' ;
LEFT                    : 'left' ;
RIGHT                   : 'right' ;
CENTER                  : 'center' ;
ABS                     : 'abs' ;
URLENCODE               : 'urlencode' ;
URLDECODE               : 'urldecode' ;
DECODE                  : 'decode' ;
ENCODE                  : 'encode' ;
BASE64                  : 'base64' ;
SPATH                   : 'spath' ;
BOOLEAN                 : 'TRUE' | 'True' | 'true' | 'FALSE' | 'False' | 'false' ;
SECONDS                 : ('second' | 'seconds') ;
MINUTES                 : ('minute' | 'minutes') ;
HOURS                   : ('hour' | 'hours') ;
DAYS                    : ('day' | 'days') ;
WEEKS                   : ('week' | 'weeks') ;
YEARS                   : ('year' | 'years') ;
// ── Time / datetime functions (reachable via eval + where) ──
NOW                     : 'now' ;
RELATIVE_TIME           : 'relative_time' ;
STRFTIME                : 'strftime' ;
STRPTIME                : 'strptime' ;
// ── String / transform functions ───────────────────────────
SPLIT                   : 'split' ;
RANDOMIZE               : 'randomize' ;
BASE64_ENCODE           : 'base64_encode' ;
BASE64_DECODE           : 'base64_decode' ;
// ── Multi-value sort ───────────────────────────────────────
MVSORT                  : 'mvsort' ;
BACKTICK                : '`' ;

// Time-bound value form for unquoted earliest=/latest= bounds.
// MUST come before NUMBER so the lexer's longest-match wins it for inputs
// like ``-1h`` (NUMBER would only match ``-1``, leaving an orphan ``h``)
// and ``2026-05-01`` (NUMBER would only match ``2026``, leaving orphans).
//
// Two branches:
//   1. Splunk relative time: ``-1h``, ``+30m``, ``-1d@d``, ``-7d@w``,
//      ``-1d@d/America/New_York``. Sign is optional (Splunk + our parser
//      both accept ``1h`` to mean ``-1h``).
//   2. ISO date / datetime: ``2026-05-01``, ``2026-05-06T20:00:00Z``,
//      ``2026-05-06T20:00:00-07:00``, ``2026-05-01/Europe/London``.
//      Note: ``Z`` here is matched as the literal Z UTC offset, not as
//      the variable name - TIMESPEC's date branch only emits when
//      preceded by ``YYYY-MM-DDThh:mm[:ss]``.
//
// Pure integers (epoch seconds) and pure floats fall through to NUMBER
// because TIMESPEC's relative branch requires a unit suffix and the date
// branch requires a ``-`` after the first 4 digits.
TIMESPEC
    : ('-' | '+')? [0-9]+ [smhdwMy] ('@' [smhdwMy])? ('/' [a-zA-Z_] [a-zA-Z_0-9]*)*
    | [0-9][0-9][0-9][0-9] '-' [0-9][0-9] '-' [0-9][0-9]
        ( ('T' | ' ') [0-9][0-9] ':' [0-9][0-9] (':' [0-9][0-9] ('.' [0-9]+)?)?
          ('Z' | ('+' | '-') [0-9][0-9] ':' [0-9][0-9])? )?
        ('/' [a-zA-Z_] [a-zA-Z_0-9]*)*
    ;

NUMBER                  : '-'? [0-9]+ ('.' [0-9]+)? ;
SINGLE_QUOTED_STRING    : '\'' (~['\r\n])* '\'' ;
DOUBLE_QUOTED_STRING    : '"' ( '\\' . | ~('"' | '\\' | '\r' | '\n') )* '"' ;
VARIABLE                : [a-zA-Z_] [a-zA-Z_0-9.]* ;

