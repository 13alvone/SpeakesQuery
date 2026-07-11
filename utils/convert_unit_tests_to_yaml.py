#!/usr/bin/env python3

# Define your test_queries here as shown previously
test_queries = [
    {
        "id": 0,
        "category": "TABLE",
        "title": "Basic Table Call",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
        '''
    },
    {
        "id": 1,
        "category": "TABLE",
        "title": "Basic Table Call with Timerange",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | timerange("11/20/2019 03:00:00", "11/28/2019 04:00:00", TIMESTAMP)
        '''
    },
    {
        "id": 2,
        "category": "COMMENTS",
        "title": "Basic Comments",
        "complexity": "basic",
        "query": '''
            # General comment test "This" and anything else like 3 should work fine and be ignored. 
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
        '''
    },
    {
        "id": 3,
        "category": "SEARCH",
        "title": "Basic search",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480" 
            | search x == 1
        '''
    },
    {
        "id": 4,
        "category": "SEARCH",
        "title": "Basic search with NOT Expression",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | search NOT x == 1
        '''
    },
    {
        "id": 5,
        "category": "SEARCH",
        "title": "Basic search with multiple Expressions (no commas)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | search x == 1 y==2 z==3
        '''
    },
    {
        "id": 6,
        "category": "SEARCH",
        "title": "Basic search with multiple Expressions (commas)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | search x >= 1, y<=2, z ==3
        '''
    },
    {
        "id": 7,
        "category": "WHERE",
        "title": "Basic WHERE",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | WHERE x == 1
        '''
    },
    {
        "id": 8,
        "category": "WHERE",
        "title": "Basic WHERE with NOT Expression",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | WHERE NOT x == 1
        '''
    },
    {
        "id": 9,
        "category": "WHERE",
        "title": "Basic WHERE with multiple Expressions (no commas)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | WHERE x <= 1 y!=2 z>=3
        '''
    },
    {
        "id": 10,
        "category": "WHERE",
        "title": "Basic WHERE with multiple Expressions (commas)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | WHERE x == 1, y==2, z ==3
        '''
    },
    {
        "id": 11,
        "category": "EVAL",
        "title": "Basic EVAL:: variable EQUALS NUMBER, variable EQUALS STRING, variable EQUALS BOOLEAN",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval x = 1
            | eval y = "string"
            | eval z = TRUE
        '''
    },
    {
        "id": 12,
        "category": "EVAL",
        "title": "Basic EVAL:: variable EQUALS arithmeticNumericExpr (2 values only)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval x = 1 + 2
            | eval y = 1 - 3
        '''
    },
    {
        "id": 13,
        "category": "EVAL",
        "title": "Basic EVAL:: variable EQUALS arithmeticNumericExpr "
                 "(3 or more values, addition and subtraction mixed)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval x = 1 + 2 - 1 - -2 +2 + -1
        '''
    },
    {
        "id": 14,
        "category": "EVAL",
        "title": "Basic EVAL:: variable EQUALS muldivNumericExpr (2 values only)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval x = 1 * 2
            | eval y = 1 / 2
        '''
    },
    {
        "id": 15,
        "category": "EVAL",
        "title": "Basic EVAL:: variable EQUALS muldivNumericExpr (3 or more values, multiplication and division mixed)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval x = 1 * 2 / 1 *2 *2 /14
        '''
    },
    {
        "id": 16,
        "category": "EVAL",
        "title": "Complex EVAL:: variable EQUALS muldivNumericExpr AND arithmeticNumericExpr "
                 "(DOUBLE_QUOTATION_MARKS used in place of variable)",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval "TEST" = 1 * 2 / 1 + 2 - 14 / 2
            | eval expected_x_result = -3
        '''
    },
    {
        "id": 17,
        "category": "EVAL",
        "title": "Complex EVAL:: variable EQUALS muldivNumericExpr AND arithmeticNumericExpr (with Parenthesis)",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval x = 1 * 2 / (1 + 2) - 14 / 2
            | eval expected_x_result = -6
        '''
    },
    {
        "id": 18,
        "category": "EVAL",
        "title": "Complex EVAL:: variable EQUALS muldivNumericExpr AND arithmeticNumericExpr "
                 "(with Parenthesis & Unary)",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval x = 1++ * 2++ / (1 + 2--) - 14 / 2--
            | eval expected_x_result = -11
        '''
    },
    {
        "id": 19,
        "category": "EVAL",
        "title": "Complex EVAL:: variable EQUALS muldivNumericExpr AND arithmeticNumericExpr "
                 "(with Parenthesis & Unary & TOSTRING function)",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval x = TOSTRING(1++ * 2++ / (1 + 2--) - 14 / 2--)
            | eval expected_x_result = -11
        '''
    },
    {
        "id": 20,
        "category": "CASE",
        "title": "Basic case statement with 2 simple entries and the required catchall (strings only & NO newlines)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval x = case(x == 1, "Yes", x == 2, "No", "Unknown Value, Check with Developer")
        '''
    },
    {
        "id": 21,
        "category": "CASE",
        "title": "Basic case statement with 2 simple entries and the required catchall (strings only & WITH newlines)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval x = case(
                x == 1, "Yes", 
                x == 2, "No", 
                "Unknown Value, Check with Developer")
        '''
    },
    {
        "id": 22,
        "category": "CASE",
        "title": "Complex case statement with inExpression and the required catchall "
                 "(strings & numbers & booleans WITH newlines)",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval x = case(
                x == TRUE, "Yes", 
                x IN (2, 4, 6, 7), "No", 
                x == "WHEN", "string detected",
                "Unknown Value, Check with Developer")
        '''
    },
    {
        "id": 23,
        "category": "CASE",
        "title": "Complex case statement with ALL comparisonExpression and the required catchall "
                 "(EQUALS_EQUALS | NOT_EQUALS | GT | LT | GTEQ | LTEQ)",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval x = case(
                x == 1, "Yes",
                x != 1.5, "Not a decimal probably.",
                x > 2, "Yes", 
                x >= 3, "Yes",
                x < 0, "Yes",
                x <= -1, "Yes",
                "No")
        '''
    },
    {
        "id": 24,
        "category": "STATS",
        "title": "Basic stats usage (NO newlines, values() only, NO by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | stats values(test) as test values(test2) as test2
        '''
    },
    {
        "id": 25,
        "category": "STATS",
        "title": "Basic stats usage (WITH newlines, values() only, NO by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | stats 
                values(test) as test 
                values(test2) as test2
        '''
    },
    {
        "id": 26,
        "category": "STATS",
        "title": "Basic stats usage (NO newlines, values() only, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | stats values(test) as test values(test2) as test2 by test3
        '''
    },
    {
        "id": 27,
        "category": "STATS",
        "title": "Basic stats usage (WITH newlines, values() only, NO by clause, and BY clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | stats 
                values(test) as test 
                values(test2) as test2
                by test3
        '''
    },
    {
        "id": 28,
        "category": "STATS",
        "title": "Stats with LATEST (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | stats
                latest(_time) as ltime
                by object _timestamp
            | stats latest(_timestamp)
        '''
    },
    {
        "id": 29,
        "category": "STATS",
        "title": "Stats with EARLIEST (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | stats
                earliest(_time) as ltime
                by object _timestamp
            | stats latest(_timestamp)
        '''
    },
    {
        "id": 30,
        "category": "STATS",
        "title": "Stats with FIRST (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | stats
                first(_time) as ftime
                by object _timestamp
            | stats first(_timestamp)
        '''
    },
    {
        "id": 31,
        "category": "STATS",
        "title": "Stats with LAST (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | stats
                last(_time) as last_time
                by object _timestamp
            | stats last(_timestamp)
        '''
    },
    {
        "id": 32,
        "category": "STATS",
        "title": "Stats with DCOUNT (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
        | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
        | stats
            dcount(_time) as dc_time
            by object _timestamp
        | stats dcount(_timestamp)
    '''
    },
    {
        "id": 33,
        "category": "STATS",
        "title": "Stats with ROUND (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | stats
                round(some_number, 2) as rounded_number
                by object some_other_number
            | stats round(some_other_number, 3)
        '''
    },
    {
        "id": 34,
        "category": "STATS",
        "title": "Stats with MIN (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | stats
                min(some_number) as min_number
                by object some_other_number
            | stats min(some_other_number)
        '''
    },
    {
        "id": 35,
        "category": "STATS",
        "title": "Stats with MAX (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
        | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
        | stats
            max(some_number) as max_number
            by object some_other_number
        | stats max(some_other_number)
    '''
    },
    {
        "id": 36,
        "category": "STATS",
        "title": "Stats with AVG (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
        | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
        | stats
            avg(some_number) as avg_number
            by object some_other_number
        | stats avg(some_other_number)
    '''
    },
    {
        "id": 37,
        "category": "STATS",
        "title": "Stats with SUM (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | stats
                sum(some_number) as summed_number
                by object some_other_number
            | stats sum(some_other_number)
        '''
    },
    {
        "id": 38,
        "category": "STATS",
        "title": "Stats with RANGE (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | stats
                range(some_number) as range_of_some_number
                by object some_other_number
            | stats range(some_other_number)
        '''
    },
    {
        "id": 39,
        "category": "STATS",
        "title": "Stats with MEDIAN (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | stats
                median(some_number) as median_of_some_number
                by object some_other_number
            | stats median(some_other_number)
        '''
    },
    {
        "id": 40,
        "category": "STATS",
        "title": "Stats with MODE (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | stats
                mode(some_number) as mode_of_some_number
                by object some_other_number
            | stats mode(some_other_number)
        '''
    },
    {
        "id": 41,
        "category": "STATS",
        "title": "Stats with MODE (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | stats
                mode(some_number) as mode_of_some_number
                by object some_other_number
            | stats mode(some_other_number)
        '''
    },
    {
        "id": 42,
        "category": "STATS",
        "title": "Stats with SQRT (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | stats
                sqrt(some_number) as sqrt_of_some_number
                by object some_other_number
            | stats sqrt(some_other_number)
        '''
    },
    {
        "id": 43,
        "category": "STATS",
        "title": "Stats with ABS (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | stats
                abs(some_number) as abs_of_some_number
                by object some_other_number
            | stats abs(some_other_number)
        '''
    },
    {
        "id": 44,
        "category": "STATS",
        "title": "Stats with RANDOM WITH NO PARAMETERS (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | stats
                random() as random_number
                by object some_other_number
            | stats random()
        '''
    },
    {
        "id": 45,
        "category": "STATS",
        "title": "Stats with RANDOM WITH start_number, end_number, and step_number (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | stats
                random(start_number, end_number, step_number) as random_number
                by object some_other_number
            | stats random(1, 10, 0.00001)
        '''
    },
    {
        "id": 46,
        "category": "EVENTSTATS",
        "title": "Basic eventstats usage (NO newlines, values() only, NO by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eventstats values(test) as test values(test2) as test2
        '''
    },
    {
        "id": 47,
        "category": "EVENTSTATS",
        "title": "Basic eventstats usage (WITH newlines, values() only, NO by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eventstats 
                values(test) as test 
                values(test2) as test2
        '''
    },
    {
        "id": 48,
        "category": "EVENTSTATS",
        "title": "Basic eventstats usage (NO newlines, values() only, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eventstats values(test) as test values(test2) as test2 by test3
        '''
    },
    {
        "id": 49,
        "category": "EVENTSTATS",
        "title": "eventstats usage (WITH newlines, values() only, NO by clause, and BY clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eventstats 
                values(test) as test 
                values(test2) as test2
                by test3
        '''
    },
    {
        "id": 50,
        "category": "EVENTSTATS",
        "title": "eventstats with LATEST (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eventstats
                latest(_time) as ltime
                by object _timestamp
            | eventstats latest(_timestamp)
        '''
    },
    {
        "id": 51,
        "category": "EVENTSTATS",
        "title": "eventstats with EARLIEST (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eventstats
                earliest(_time) as ltime
                by object _timestamp
            | eventstats latest(_timestamp)
        '''
    },
    {
        "id": 52,
        "category": "EVENTSTATS",
        "title": "eventstats with FIRST (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eventstats
                first(_time) as ftime
                by object _timestamp
            | eventstats first(_timestamp)
        '''
    },
    {
        "id": 53,
        "category": "EVENTSTATS",
        "title": "eventstats with LAST (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eventstats
                last(_time) as last_time
                by object _timestamp
            | eventstats last(_timestamp)
        '''
    },
    {
        "id": 54,
        "category": "EVENTSTATS",
        "title": "eventstats with DCOUNT (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eventstats
                dcount(_time) as dc_time
                by object _timestamp
            | eventstats dcount(_timestamp)
        '''
    },
    {
        "id": 55,
        "category": "EVENTSTATS",
        "title": "eventstats with ROUND (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eventstats
                round(some_number, 2) as rounded_number
                by object some_other_number
            | eventstats round(some_other_number, 3)
        '''
    },
    {
        "id": 56,
        "category": "EVENTSTATS",
        "title": "eventstats with MIN (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
        | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
        | eventstats
            min(some_number) as min_number
            by object some_other_number
        | eventstats min(some_other_number)
    '''
    },
    {
        "id": 57,
        "category": "EVENTSTATS",
        "title": "eventstats with MAX (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eventstats
                max(some_number) as max_number
                by object some_other_number
            | eventstats max(some_other_number)
        '''
    },
    {
        "id": 58,
        "category": "EVENTSTATS",
        "title": "eventstats with AVG (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eventstats
                avg(some_number) as avg_number
                by object some_other_number
            | eventstats avg(some_other_number)
        '''
    },
    {
        "id": 59,
        "category": "EVENTSTATS",
        "title": "eventstats with SUM (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eventstats
                sum(some_number) as summed_number
                by object some_other_number
            | eventstats sum(some_other_number)
        '''
    },
    {
        "id": 60,
        "category": "EVENTSTATS",
        "title": "eventstats with RANGE (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eventstats
                range(some_number) as range_of_some_number
                by object some_other_number
            | eventstats range(some_other_number)
        '''
    },
    {
        "id": 61,
        "category": "EVENTSTATS",
        "title": "eventstats with MEDIAN (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eventstats
                median(some_number) as median_of_some_number
                by object some_other_number
            | eventstats median(some_other_number)
        '''
    },
    {
        "id": 62,
        "category": "EVENTSTATS",
        "title": "eventstats with MODE (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eventstats
                mode(some_number) as mode_of_some_number
                by object some_other_number
            | eventstats mode(some_other_number)
        '''
    },
    {
        "id": 63,
        "category": "EVENTSTATS",
        "title": "eventstats with MODE (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eventstats
                mode(some_number) as mode_of_some_number
                by object some_other_number
            | eventstats mode(some_other_number)
        '''
    },
    {
        "id": 64,
        "category": "EVENTSTATS",
        "title": "eventstats with SQRT (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eventstats
                sqrt(some_number) as sqrt_of_some_number
                by object some_other_number
            | eventstats sqrt(some_other_number)
        '''
    },
    {
        "id": 65,
        "category": "EVENTSTATS",
        "title": "eventstats with ABS (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eventstats
                abs(some_number) as abs_of_some_number
                by object some_other_number
            | eventstats abs(some_other_number)
        '''
    },
    {
        "id": 66,
        "category": "EVENTSTATS",
        "title": "Stats with RANDOM WITH NO PARAMETERS (WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eventstats
                random() as random_number
                by object some_other_number
            | eventstats random()
        '''
    },
    {
        "id": 67,
        "category": "EVENTSTATS",
        "title": "eventstats with RANDOM WITH start_number, end_number, and step_number "
                 "(WITH newlines, WITH by clause)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eventstats
                random(start_number, end_number, step_number) as random_number
                by object some_other_number
            | eventstats random(1, 10, 0.00001)
        '''
    },
    {
        "id": 68,
        "category": "RENAME",
        "title": "Basic RENAME with single variable to variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | rename oldVar AS newVar
        '''
    },
    {
        "id": 69,
        "category": "RENAME",
        "title": "Basic RENAME with single variable to double-quoted string",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | rename oldVar AS "New Variable Name"
        '''
    },
    {
        "id": 70,
        "category": "RENAME",
        "title": "RENAME with multiple variables to variables without commas",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | rename oldVar1 AS newVar1 oldVar2 AS newVar2
        '''
    },
    {
        "id": 71,
        "category": "RENAME",
        "title": "RENAME with multiple variables to double-quoted strings without commas",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | rename oldVar1 AS "New Var 1" oldVar2 AS "New Var 2"
        '''
    },
    {
        "id": 72,
        "category": "RENAME",
        "title": "RENAME with multiple variables, mixed variable and double-quoted string targets without commas",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | rename oldVar1 AS newVar1 oldVar2 AS "New Var 2"
        '''
    },
    {
        "id": 73,
        "category": "RENAME",
        "title": "RENAME with multiple variables to variables with commas",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | rename oldVar1 AS newVar1, oldVar2 AS newVar2
        '''
    },
    {
        "id": 74,
        "category": "RENAME",
        "title": "RENAME with multiple variables to double-quoted strings with commas",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | rename oldVar1 AS "New Var 1", oldVar2 AS "New Var 2"
        '''
    },
    {
        "id": 75,
        "category": "RENAME",
        "title": "RENAME with multiple variables, mixed variable and double-quoted string targets with commas",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | rename oldVar1 AS newVar1, oldVar2 AS "New Var 2"
        '''
    },
    {
        "id": 76,
        "category": "RENAME",
        "title": "Complex RENAME with many variables to variables mixed with double-quoted strings",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | rename oldVar1 AS newVar1, oldVar2 AS "New Var 2", oldVar3 AS newVar3, oldVar4 AS "New Var 4"
        '''
    },
    {
        "id": 77,
        "category": "FIELDS",
        "title": "FIELDS with a single variable (implicitly adding)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | fields myVar1
        '''
    },
    {
        "id": 78,
        "category": "FIELDS",
        "title": "FIELDS excluding a single variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | fields - myVar1
        '''
    },
    {
        "id": 79,
        "category": "FIELDS",
        "title": "FIELDS including a single variable explicitly",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | fields + myVar1
        '''
    },
    {
        "id": 80,
        "category": "FIELDS",
        "title": "FIELDS with multiple variables without commas (implicitly adding)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | fields myVar1 myVar2 myVar3
        '''
    },
    {
        "id": 81,
        "category": "FIELDS",
        "title": "FIELDS excluding multiple variables without commas",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | fields - myVar1 myVar2 myVar3
        '''
    },
    {
        "id": 82,
        "category": "FIELDS",
        "title": "FIELDS including multiple variables explicitly without commas",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | fields + myVar1 myVar2 myVar3
        '''
    },
    {
        "id": 83,
        "category": "FIELDS",
        "title": "FIELDS with multiple variables with commas (implicitly adding)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | fields myVar1, myVar2, myVar3
        '''
    },
    {
        "id": 84,
        "category": "FIELDS",
        "title": "FIELDS excluding multiple variables with commas",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | fields - myVar1, myVar2, myVar3
        '''
    },
    {
        "id": 85,
        "category": "FIELDS",
        "title": "FIELDS including multiple variables explicitly with commas",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | fields + myVar1, myVar2, myVar3
        '''
    },
    {
        "id": 86,
        "category": "MAKETABLE",
        "title": "MAKETABLE with a single variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | maketable myVar1
        '''
    },
    {
        "id": 87,
        "category": "MAKETABLE",
        "title": "MAKETABLE with two variables without commas",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | maketable myVar1 myVar2
        '''
    },
    {
        "id": 88,
        "category": "MAKETABLE",
        "title": "MAKETABLE with three variables without commas",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | maketable myVar1 myVar2 myVar3
        '''
    },
    {
        "id": 89,
        "category": "MAKETABLE",
        "title": "MAKETABLE with two variables with commas",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | maketable myVar1, myVar2
        '''
    },
    {
        "id": 90,
        "category": "MAKETABLE",
        "title": "MAKETABLE with three variables with commas",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | maketable myVar1, myVar2, myVar3
        '''
    },
    {
        "id": 91,
        "category": "MAKETABLE",
        "title": "MAKETABLE with multiple variables, mixed with and without commas",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | maketable myVar1, myVar2 myVar3
        '''
    },
    {
        "id": 92,
        "category": "LOOKUP",
        "title": "LOOKUP with file and variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | lookup file="lookupTable.csv" myVar
        '''
    },
    {
        "id": 93,
        "category": "LOOKUP",
        "title": "LOOKUP with file and variable, using AS for aliasing to another variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | lookup file="lookupTable.csv" myVar AS newVar
        '''
    },
    {
        "id": 94,
        "category": "LOOKUP",
        "title": "LOOKUP with file and variable, using AS for aliasing to a double-quoted string",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | lookup file="lookupTable.csv" myVar AS "New Variable Name"
        '''
    },
    {
        "id": 95,
        "category": "LOOKUP",
        "title": "LOOKUP with file, variable and OUTPUTNEW to a new variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | lookup file="lookupTable.csv" myVar OUTPUTNEW resultVar
        '''
    },
    {
        "id": 96,
        "category": "LOOKUP",
        "title": "LOOKUP with file, variable and OUTPUTNEW to a double-quoted string",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | lookup file="lookupTable.csv" myVar OUTPUTNEW "Result Variable"
        '''
    },
    {
        "id": 97,
        "category": "LOOKUP",
        "title": "LOOKUP with file, variable using AS and OUTPUTNEW combined to new variables",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | lookup file="lookupTable.csv" myVar AS newVar OUTPUTNEW outputVar
        '''
    },
    {
        "id": 98,
        "category": "LOOKUP",
        "title": "LOOKUP with file, variable using AS and OUTPUTNEW combined to a double-quoted string",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | lookup file="lookupTable.csv" myVar AS newVar OUTPUTNEW "Output Variable"
        '''
    },
    {
        "id": 99,
        "category": "HEAD/LIMIT",
        "title": "HEAD with a small number",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | head 5
        '''
    },
    {
        "id": 100,
        "category": "HEAD/LIMIT",
        "title": "HEAD with a larger number",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | head 100
        '''
    },
    {
        "id": 101,
        "category": "HEAD/LIMIT",
        "title": "LIMIT with a small number",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | limit 10
        '''
    },
    {
        "id": 102,
        "category": "HEAD/LIMIT",
        "title": "LIMIT with a very large number",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | limit 1000
        '''
    },
    {
        "id": 103,
        "category": "HEAD/LIMIT",
        "title": "HEAD with the minimum allowable number (assuming 1)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | head 1
        '''
    },
    {
        "id": 104,
        "category": "HEAD/LIMIT",
        "title": "LIMIT with the minimum allowable number (assuming 1)",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | limit 1
        '''
    },
    {
        "id": 105,
        "category": "BIN",
        "title": "BIN with a simple time interval",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | bin myVar span=1h
        '''
    },
    {
        "id": 106,
        "category": "BIN",
        "title": "BIN with a more precise time interval",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | bin timestamp span=15m
        '''
    },
    {
        "id": 107,
        "category": "BIN",
        "title": "BIN with a day time interval",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | bin eventTime span=1d
        '''
    },
    {
        "id": 108,
        "category": "BIN",
        "title": "BIN with a week time interval",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | bin eventWeek span=1w
        '''
    },
    {
        "id": 109,
        "category": "BIN",
        "title": "BIN with a minute time interval",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | bin eventMinute span=1m
        '''
    },
    {
        "id": 110,
        "category": "BIN",
        "title": "BIN with a month time interval",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | bin eventMonth span=1mon
        '''
    },
    {
        "id": 111,
        "category": "REVERSE",
        "title": "REVERSE command usage",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | reverse
        '''
    },
    {
        "id": 112,
        "category": "DEDUP",
        "title": "DEDUP command usage",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | dedup myVar
        '''
    },
    {
        "id": 113,
        "category": "SORT",
        "title": "SORT with ascending order",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | sort + myVar
        '''
    },
    {
        "id": 114,
        "category": "SORT",
        "title": "SORT with descending order",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | sort - myVar
        '''
    },
    {
        "id": 115,
        "category": "SORT",
        "title": "SORT with multiple variables ascending and commas",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | sort + myVar1, myVar2
        '''
    },
    {
        "id": 116,
        "category": "SORT",
        "title": "SORT with multiple variables descending, no commmas",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | sort - myVar1 myVar2
        '''
    },
    {
        "id": 117,
        "category": "REX",
        "title": "REX basic usage for field extraction",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | rex field=myRawField "=error code (?<errorCode>\\d+)"
        '''
    },
    {
        "id": 118,
        "category": "REX",
        "title": "REX with SED mode for simple substitution, no extractions",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | rex field=myRawField mode=sed "s/error code \\d+/Error Detected/"
        '''
    },
    {
        "id": 119,
        "category": "REX",
        "title": "REX for complex pattern extraction",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | rex field=myRawField "=error code (?<errorCode>\\d+) occurred at (?<timeStamp>\\d{2}:\\d{2})"
        '''
    },
    {
        "id": 120,
        "category": "REX",
        "title": "REX with SED mode for complex substitution, no extractions",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | rex field=myRawField mode=sed "s/(error code \\d+).+?(user \\w+)/\\1, critical error detected/"
        '''
    },
    {
        "id": 121,
        "category": "REGEX",
        "title": "REGEX simple match",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | regex myVar = "error code \\d+"
        '''
    },
    {
        "id": 122,
        "category": "REGEX",
        "title": "REGEX simple non-match",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | regex myVar != "success"
        '''
    },
    {
        "id": 123,
        "category": "REGEX",
        "title": "REGEX match with special characters",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | regex myVar = "^Start\\s+.*end$"
        '''
    },
    {
        "id": 124,
        "category": "REGEX",
        "title": "REGEX non-match with variable comparison",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | regex myVar != anotherVar
        '''
    },
    {
        "id": 125,
        "category": "REGEX",
        "title": "REGEX match with complex pattern",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | regex myVar = "\\b(?:error|warn|fail)\\b"
        '''
    },
    {
        "id": 127,
        "category": "BASE64",
        "title": "BASE64 encode a single variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | base64 encode myVar
        '''
    },
    {
        "id": 128,
        "category": "BASE64",
        "title": "BASE64 decode a single variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | base64 decode myVar
        '''
    },
    {
        "id": 129,
        "category": "BASE64",
        "title": "BASE64 encode multiple variables",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | base64 encode myVar1, myVar2
        '''
    },
    {
        "id": 130,
        "category": "BASE64",
        "title": "BASE64 decode multiple variables",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | base64 decode myVar1, myVar2
        '''
    },
    {
        "id": 131,
        "category": "BASE64",
        "title": "BASE64 encode a literal string",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | base64 encode "Hello, World!"
        '''
    },
    {
        "id": 132,
        "category": "BASE64",
        "title": "BASE64 decode a literal string",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | base64 decode "SGVsbG8sIFdvcmxkIQ=="
        '''
    },
    {
        "id": 133,
        "category": "BASE64",
        "title": "BASE64 encode mixed variables and literal strings",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | base64 encode myVar, "Sample Text"
        '''
    },
    {
        "id": 134,
        "category": "BASE64",
        "title": "BASE64 decode mixed variables and literal strings",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | base64 decode myVar, "U2FtcGxlIFRleHQ="
        '''
    },
    {
        "id": 135,
        "category": "SPECIAL_FUNCTION",
        "title": "SPECIAL_FUNCTION with a simple ID",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | special_function `simpleFunction`
        '''
    },
    {
        "id": 136,
        "category": "SPECIAL_FUNCTION",
        "title": "SPECIAL_FUNCTION with a numeric ID",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | special_function `123`
        '''
    },
    {
        "id": 137,
        "category": "SPECIAL_FUNCTION",
        "title": "SPECIAL_FUNCTION with a complex ID including underscores",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | special_function `complex_function_id_456`
        '''
    },
    {
        "id": 138,
        "category": "SPECIAL_FUNCTION",
        "title": "SPECIAL_FUNCTION with an ID containing numbers and letters",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | special_function `func123ID`
        '''
    },
    {
        "id": 139,
        "category": "FILLNULL",
        "title": "FILLNULL for a single variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | fillnull value=myDefaultVar myVar1
        '''
    },
    {
        "id": 140,
        "category": "FILLNULL",
        "title": "FILLNULL for multiple variables without commas",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | fillnull value=myDefaultVar myVar1 myVar2 myVar3
        '''
    },
    {
        "id": 141,
        "category": "FILLNULL",
        "title": "FILLNULL for multiple variables with commas",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | fillnull value=myDefaultVar myVar1, myVar2, myVar3
        '''
    },
    {
        "id": 142,
        "category": "FILLNULL",
        "title": "FILLNULL with a string as a value for multiple variables",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | fillnull value="defaultString" myVar1, myVar2
        '''
    },
    {
        "id": 143,
        "category": "FILLNULL",
        "title": "FILLNULL with a numeric value for multiple variables",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | fillnull value=0 myVar1 myVar2
        '''
    },
    {
        "id": 144,
        "category": "LOADJOB",
        "title": "LOADJOB with a simple job ID",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | loadjob "12345.6789"
        '''
    },
    {
        "id": 145,
        "category": "LOADJOB",
        "title": "LOADJOB with a complex job ID",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | loadjob "scheduler__admin__search__RMD5c8d8e7d622d3d7b8_1582231200_11"
        '''
    },
    {
        "id": 146,
        "category": "INPUTLOOKUP",
        "title": "INPUTLOOKUP with a CSV file",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | inputlookup "myLookupTable.csv"
        '''
    },
    {
        "id": 147,
        "category": "INPUTLOOKUP",
        "title": "INPUTLOOKUP with a KV Store collection",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | inputlookup "myKVStoreCollection"
        '''
    },
    {
        "id": 148,
        "category": "SPATH",
        "title": "SPATH to extract a field from JSON",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | spath myJsonField
        '''
    },
    {
        "id": 149,
        "category": "SPATH",
        "title": "SPATH to extract a nested field from JSON",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | spath myNestedJsonField
        '''
    },
    {
        "id": 150,
        "category": "OUTPUTLOOKUP",
        "title": "OUTPUTLOOKUP to a file without any flags",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | outputlookup "myOutputLookup.csv"
        '''
    },
    {
        "id": 151,
        "category": "OUTPUTLOOKUP",
        "title": "OUTPUTLOOKUP with OVERWRITE enabled",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | outputlookup overwrite=true "myOutputLookup.csv"
        '''
    },
    {
        "id": 152,
        "category": "OUTPUTLOOKUP",
        "title": "OUTPUTLOOKUP with CREATE_EMPTY enabled",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | outputlookup create_empty=true "myNewLookup.csv"
        '''
    },
    {
        "id": 153,
        "category": "OUTPUTLOOKUP",
        "title": "OUTPUTLOOKUP with OVERWRITE_IF_EMPTY enabled",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | outputlookup overwrite_if_empty=true "myConditionalOutputLookup.csv"
        '''
    },
    {
        "id": 154,
        "category": "OUTPUTLOOKUP",
        "title": "OUTPUTLOOKUP with all flags enabled",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | outputlookup overwrite=true create_empty=true overwrite_if_empty=true "myFullyFlaggedOutputLookup.csv"
        '''
    },
    {
        "id": 155,
        "category": "JOIN",
        "title": "JOIN with LEFT type and basic table lookup",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | join type=left userID [
                | table "userData.csv"]
        '''
    },
    {
        "id": 156,
        "category": "JOIN",
        "title": "JOIN with CENTER type and table lookup with search",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | join type=center errorID [
                | table "serverLogs.csv"
                | search error_code = 404]
        '''
    },
    {
        "id": 157,
        "category": "JOIN",
        "title": "JOIN with RIGHT type, dedup and sort operations",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | join type=right userID [
                | table "customerData.csv" 
                | dedup userID 
                | sort +signupDate]
        '''
    },
    {
        "id": 158,
        "category": "JOIN",
        "title": "JOIN with LEFT type, rex extraction and base64 encoding",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | join type=left transactionID [
                | table "transactionRecords.csv" 
                | rex field=transactionData "=amount:(?<amount>\\d+)"
                | base64 encode amount]
        '''
    },
    {
        "id": 159,
        "category": "JOIN",
        "title": "JOIN with CENTER type, fillnull, eval case, and outputlookup",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | join type=center productID [
                | table "inventory.csv" 
                | fillnull value=0 stockCount 
                | eval status = case(stockCount < 10, "Low", stockCount <= 20, "Medium", "High") 
                | outputlookup "inventoryStatus.csv"]
        '''
    },
    {
        "id": 160,
        "category": "MIN",
        "title": "MIN of a single variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | min(myVar)
        '''
    },
    {
        "id": 161,
        "category": "MAX",
        "title": "MAX of a variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | max(anotherVar)
        '''
    },
    {
        "id": 162,
        "category": "AVG",
        "title": "AVG of a variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | avg(scoreVar)
        '''
    },
    {
        "id": 163,
        "category": "SUM",
        "title": "SUM of a variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | sum(revenueVar)
        '''
    },
    {
        "id": 164,
        "category": "RANGE",
        "title": "RANGE of a variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | range(tempVar)
        '''
    },
    {
        "id": 165,
        "category": "MEDIAN",
        "title": "MEDIAN of a variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | median(populationVar)
        '''
    },
    {
        "id": 166,
        "category": "MODE",
        "title": "MODE of a variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | mode(dayVar)
        '''
    },
    {
        "id": 167,
        "category": "SQRT",
        "title": "SQRT of a variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | sqrt(areaVar)
        '''
    },
    {
        "id": 168,
        "category": "ABS",
        "title": "ABS of a variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | abs(profitLossVar)
        '''
    },
    {
        "id": 169,
        "category": "ROUND",
        "title": "ROUND a variable to nearest whole number",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | round(valueVar, precisionVar)
        '''
    },
    {
        "id": 170,
        "category": "ROUND",
        "title": "ROUND a variable to two decimal places",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | round(decimalVar, 2)
        '''
    },
    {
        "id": 171,
        "category": "ROUND",
        "title": "ROUND a variable with dynamic precision from another variable",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | round(amountVar, round(precisionLevelVar / 2, 0))
        '''
    },
    {
        "id": 172,
        "category": "ROUND",
        "title": "ROUND a negative variable to nearest whole number",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | round(negativeVar, 0)
        '''
    },
    {
        "id": 173,
        "category": "ROUND",
        "title": "ROUND a variable with high precision specified",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | round(highPrecisionVar - degradation_number, 4)
        '''
    },
    {
        "id": 174,
        "category": "RANDOM",
        "title": "RANDOM without parameters",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | random()
        '''
    },
    {
        "id": 175,
        "category": "RANDOM",
        "title": "RANDOM with start, end, and fidelity variables",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | random(startVar, endVar, fidelityVar)
        '''
    },
    {
        "id": 176,
        "category": "RANDOM",
        "title": "RANDOM with numeric values for start, end, and fidelity",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | random(1, 100, 10)
        '''
    },
    {
        "id": 177,
        "category": "RANDOM",
        "title": "RANDOM with mixed variables and numeric expressions",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | random(startVar, 50*2, fidelityVar)
        '''
    },
    {
        "id": 178,
        "category": "RANDOM",
        "title": "RANDOM with mixed variables and numeric expressions that also have variables",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | random(startVar, 50*fidelityVar, fidelityVar)
        '''
    },
    {
        "id": 179,
        "category": "RANDOM",
        "title": "RANDOM with all numeric expressions",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | random(10+2, 200-50, 5*3)
        '''
    },
    {
        "id": 180,
        "category": "UPPER",
        "title": "UPPER to convert a variable to uppercase",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | upper(myStringVar)
            | upper("test")
        '''
    },
    {
        "id": 181,
        "category": "LOWER",
        "title": "LOWER to convert a string to lowercase",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | lower(myStringVar)
            | lower("HELLO WORLD")
        '''
    },
    {
        "id": 182,
        "category": "CAPITALIZE",
        "title": "CAPITALIZE to capitalize a variable string",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | capitalize(myStringVar)
            | capitalize("this word")
        '''
    },
    {
        "id": 183,
        "category": "LEN",
        "title": "LEN to get the length of a string variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | len(myStringVar)
            | len("this should work.")
        '''
    },
    {
        "id": 184,
        "category": "TOSTRING",
        "title": "TOSTRING to convert a numeric variable to string",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | tostring(myNumericVar)
            | tostring(4)
        '''
    },
    {
        "id": 185,
        "category": "URLENCODE",
        "title": "URLENCODE to encode a string variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | urlencode(myStringVar)
            | urlencode("http://test.com")
        '''
    },
    {
        "id": 186,
        "category": "URLDECODE",
        "title": "URLDECODE to decode an encoded string variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | urldecode(myEncodedVar)
            | urldecode("http%3A%2F%2Ftest.com")
        '''
    },
    {
        "id": 187,
        "category": "DEFANG",
        "title": "DEFANG to defang a URL contained in a variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | defang(myUrlVar)
            | defang(http://test.com)
        '''
    },
    {
        "id": 188,
        "category": "TRIM",
        "title": "TRIM whitespace from a variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | trim(myStringVar)
        '''
    },
    {
        "id": 189,
        "category": "TRIM",
        "title": "TRIM specific characters from a string variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | trim(myStringVar, ",. ")
        '''
    },
    {
        "id": 190,
        "category": "RTRIM",
        "title": "RTRIM whitespace from a string literal",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | rtrim(" Hello World ")
        '''
    },
    {
        "id": 191,
        "category": "RTRIM",
        "title": "RTRIM specific characters from a string variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | rtrim(myStringVar, "!. ")
        '''
    },
    {
        "id": 192,
        "category": "LTRIM",
        "title": "LTRIM whitespace from a string literal",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | ltrim(" Hello World ")
        '''
    },
    {
        "id": 193,
        "category": "LTRIM",
        "title": "LTRIM specific characters from a string variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | ltrim(myStringVar, "Hd")
        '''
    },
    {
        "id": 194,
        "category": "TRIM & RTRIM",
        "title": "Combining TRIM and RTRIM on separate variables",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | rtrim(trim(myStringVar))
        '''
    },
    {
        "id": 195,
        "category": "LTRIM & RTRIM",
        "title": "Sequentially applying LTRIM and RTRIM to the same variable",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | rtrim(ltrim(myStringVar, "this"))
        '''
    },
    {
        "id": 196,
        "category": "TRIM & LTRIM & RTRIM",
        "title": "Applying TRIM, then LTRIM, and finally RTRIM on the same string",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | rtrim(ltrim(trim(myStringVar), "Prefix"), "Suffix")
        '''
    },
    {
        "id": 197,
        "category": "LTRIM & TRIM",
        "title": "LTRIM followed by TRIM for specific character removal and whitespace cleanup",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | trim(ltrim(myStringVar, "0"))
        '''
    },
    {
        "id": 198,
        "category": "RTRIM & TRIM",
        "title": "RTRIM to remove specific characters then TRIM to clean up remaining whitespace",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | trim(rtrim(myStringVar, "!"))
        '''
    },

    {
        "id": 199,
        "category": "CONCAT",
        "title": "CONCAT two string variables",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | concat(varOne, varTwo)
        '''
    },
    {
        "id": 200,
        "category": "CONCAT",
        "title": "CONCAT a variable and a string literal",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | concat(myVar, " is a string literal")
        '''
    },
    {
        "id": 201,
        "category": "CONCAT",
        "title": "CONCAT multiple string literals",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | concat("Hello, ", "world", "! How are you?")
        '''
    },
    {
        "id": 202,
        "category": "CONCAT",
        "title": "CONCAT a mix of variables and string literals",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | concat("User: ", userName, ", Email: ", userEmail)
        '''
    },
    {
        "id": 203,
        "category": "CONCAT",
        "title": "CONCAT several variables into a single string",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | concat(firstName, " ", lastName, " lives in ", 2, " places within", city, ", ", country)
        '''
    },
    {
        "id": 204,
        "category": "SUBSTR",
        "title": "SUBSTR from a variable starting position",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | substr(myString, startPositionVar, 45)
        '''
    },
    {
        "id": 205,
        "category": "SUBSTR",
        "title": "SUBSTR from a string literal with start and end positions",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | substr("Hello, World!", 7, 5)
        '''
    },
    {
        "id": 206,
        "category": "SUBSTR",
        "title": "SUBSTR from a variable with all parameters as variables",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | substr(sourceVar, startVar, lengthVar)
        '''
    },
    {
        "id": 207,
        "category": "SUBSTR",
        "title": "SUBSTR from a string literal, specifying only the start",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | substr("Extract this substring.", 8, 10)
        '''
    },
    {
        "id": 208,
        "category": "SUBSTR",
        "title": "SUBSTR from a variable, specifying start and length as literals",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | substr(myVariable, 0, 10)
        '''
    },
    {
        "id": 209,
        "category": "MATCH",
        "title": "MATCH to check pattern in a variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | match(myStringVar, "pattern")
        '''
    },
    {
        "id": 210,
        "category": "MATCH",
        "title": "NOT MATCH to verify pattern is not in a string literal",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | not match("This is a test.", "^No match here$")
        '''
    },
    {
        "id": 211,
        "category": "MATCH",
        "title": "MATCH with regular expression in a variable",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | match(logEntry, regexPatternVar)
        '''
    },
    {
        "id": 212,
        "category": "MATCH",
        "title": "NOT MATCH using variable against a fixed pattern",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | not match(emailVar, "@example.com$")
        '''
    },
    {
        "id": 213,
        "category": "MATCH",
        "title": "MATCH to find substring in a variable using literal pattern",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | match(userNameVar, "user_")
        '''
    },
    {
        "id": 214,
        "category": "REPLACE",
        "title": "REPLACE in a variable with literal strings",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | replace(myTextVar, "old", "new")
        '''
    },
    {
        "id": 215,
        "category": "REPLACE",
        "title": "REPLACE in a string literal using variables for pattern and replacement",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | replace("Sample old text.", patternVar, replacementVar)
        '''
    },
    {
        "id": 216,
        "category": "REPLACE",
        "title": "REPLACE using variables for all parameters",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | replace(sourceVar, findVar, replaceWithVar)
        '''
    },
    {
        "id": 217,
        "category": "REPLACE",
        "title": "REPLACE in a variable, complex pattern and replacement",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | replace(logEntryVar, "[ERROR]", "[INFO]")
        '''
    },
    {
        "id": 218,
        "category": "REPLACE",
        "title": "REPLACE multiple occurrences in a variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | replace(messageVar, " ", "_")
        '''
    },
    {
        "id": 219,
        "category": "NULL",
        "title": "NULL command usage",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | null()
        '''
    },
    {
        "id": 220,
        "category": "ISNULL",
        "title": "ISNULL to check a variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | isnull(myVar)
        '''
    },
    {
        "id": 221,
        "category": "ISNULL",
        "title": "NOT ISNULL to validate variable is not null",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | not isnull(myVar)
        '''
    },
    {
        "id": 222,
        "category": "TO_CRON",
        "title": "TO_CRON to convert a natural language expression to CRON",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | to_cron("every day at midnight")
        '''
    },
    {
        "id": 223,
        "category": "FROM_CRON",
        "title": "FROM_CRON to interpret a CRON expression",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | from_cron("0 0 * * *", "GMT")
        '''
    },
    {
        "id": 224,
        "category": "FIELDSUMMARY",
        "title": "FIELDSUMMARY to generate field statistics",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | fieldsummary()
        '''
    },
    {
        "id": 225,
        "category": "COALESCE",
        "title": "COALESCE to select the first non-null variable",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | coalesce(var1, var2, var3)
        '''
    },
    {
        "id": 226,
        "category": "MVJOIN",
        "title": "MVJOIN to concatenate multivalue fields",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | mvjoin(myMultivalueVar, ", ")
        '''
    },
    {
        "id": 227,
        "category": "MVINDEX",
        "title": "MVINDEX to access a specific value in a multivalue field",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | mvindex(myMultivalueVar, 1)
        '''
    },
    {
        "id": 228,
        "category": "MVREVERSE",
        "title": "MVREVERSE to reverse the order of values in a multivalue field",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | mvreverse(myMultivalueVar)
        '''
    },
    {
        "id": 229,
        "category": "MVFIND",
        "title": "MVFIND to find a string within a multivalue field",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | mvfind(myMultivalueVar, "searchString")
        '''
    },
    {
        "id": 230,
        "category": "MVDEDUP",
        "title": "MVDEDUP to deduplicate entries in a multivalue field",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | mvdedup(myMultivalueVar)
        '''
    },
    {
        "id": 231,
        "category": "MVAPPEND",
        "title": "MVAPPEND to add new values to a multivalue field",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | mvappend(myMultivalueVar, "newValue1", "newValue2")
        '''
    },
    {
        "id": 232,
        "category": "MVFILTER",
        "title": "MVFILTER to remove items from a multivalue field based on a condition",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | mvfilter(myMultivalueVar != "removeThis")
        '''
    },
    {
        "id": 233,
        "category": "MVEXPAND",
        "title": "MVEXPAND to expand a multivalue field into separate events",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | mvexpand(myMultivalueVar)
        '''
    },
    {
        "id": 234,
        "category": "MVCOMBINE",
        "title": "MVCOMBINE to concatenate multivalue entries with a delimiter",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | mvcombine(myMultivalueVar, ",")
        '''
    },
    {
        "id": 235,
        "category": "MVCOUNT",
        "title": "MVCOUNT to count the number of items in a multivalue field",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | mvcount(myMultivalueVar)
        '''
    },
    {
        "id": 236,
        "category": "MVDCOUNT",
        "title": "MVDCOUNT for distinct count of multivalue field entries",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | mvcount(distinct myMultivalueVar)
        '''
    },
    {
        "id": 237,
        "category": "MVZIP",
        "title": "MVZIP to join two multivalue fields by a delimiter",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | mvzip(myMultivalueVar1, myMultivalueVar2, ";")
        '''
    },
    {
        "id": 238,
        "category": "MACRO",
        "title": "MACRO to execute a reusable code segment",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | macro(myReusableCode)
        '''
    },
    {
        "id": 239,
        "category": "STREAMSTATS",
        "title": "Compute moving average over a series of events",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | streamstats window=3 values(bytes) as movingAvgBytes
        '''
    },
    {
        "id": 240,
        "category": "STREAMSTATS",
        "title": "Calculate running total of bytes by host",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | streamstats sum(bytes) AS totalBytes BY host
        '''
    },
    {
        "id": 241,
        "category": "STREAMSTATS",
        "title": "Running total with reset after specific action",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | streamstats sum(bytes) AS totalBytes BY host reset after action="REBOOT"
        '''
    },
    {
        "id": 242,
        "category": "STREAMSTATS",
        "title": "Applying a count to each event",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | streamstats count()
        '''
    },
    {
        "id": 243,
        "category": "STREAMSTATS",
        "title": "Compute moving average excluding the current event",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | streamstats current=false window=3 values(bytes) as movingAvgBytesExclCurrent
        '''
    },
    {
        "id": 244,
        "category": "STREAMSTATS",
        "title": "Running total by host with reset after REBOOT, excluding current event",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | streamstats current=false sum(bytes) AS totalBytes BY host reset after action="REBOOT"
        '''
    },
    {
        "id": 245,
        "category": "STREAMSTATS",
        "title": "Running total with multiple resets conditions",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | streamstats sum(bytes) AS totalBytes BY host reset after action="REBOOT" reset after action="LOGOFF"
        '''
    },
    {
        "id": 246,
        "category": "STREAMSTATS",
        "title": "Compute distinct count of actions by host",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | streamstats count() as actionCount BY host
        '''
    },
    {
        "id": 247,
        "category": "STREAMSTATS",
        "title": "Compute moving average with dynamic window size",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | streamstats window=dynamicWindowSize values(bytes) as movingAvgBytesDynamicWindow
        '''
    },
    {
        "id": 248,
        "category": "STREAMSTATS",
        "title": "Reset running total based on a numeric condition",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | streamstats sum(bytes) AS totalBytes BY host reset_after bytes=1024
        '''
    },
    {
        "id": 249,
        "category": "STREAMSTATS",
        "title": "Apply multiple aggregations with different reset conditions",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | streamstats sum(bytes) as totalBytes, avg(bytes) as avgBytes BY host reset_after action="REBOOT"
        '''
    },
    {
        "id": 250,
        "category": "STREAMSTATS",
        "title": "Combining CURRENT, WINDOW, BY, and RESET_AFTER options",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | streamstats current=true window=5 values(bytes) as recentBytes BY host ip_address reset_after action="LOGOFF"
        '''
    },
    {
        "id": 251,
        "category": "STREAMSTATS",
        "title": "Using AS to rename aggregation results, with COUNT and BY clause",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | streamstats values(bytes) as bytesValues count as eventCount BY host ip_address
        '''
    },
    {
        "id": 252,
        "category": "STREAMSTATS",
        "title": "Dynamic WINDOW size based on another field, with BY clause",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | streamstats window=dynamicWindowField values(bytes) as bytesWindow BY host
        '''
    },
    {
        "id": 253,
        "category": "IF",
        "title": "Using ifExpression within eval for simple comparison",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval result = if(x + 13 == 14, "yes", "no")
        '''
    },
    {
        "id": 254,
        "category": "IF",
        "title": "Using inExpression within eval to check inclusion",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval included = if(status IN ("start", "finish", "test"), "yes", "no")
        '''
    },
    {
        "id": 255,
        "category": "IF",
        "title": "Using NOT with inExpression within eval",
        "complexity": "basic",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval notIncluded = if(NOT status IN ("pending", "cancelled"), "proceed", "halt")
        '''
    },
    {
        "id": 259,
        "category": "IF",
        "title": "Check multiple inclusion conditions within eval",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval multiCheck = if(action IN ("login", "logout") AND device IN ("mobile", "tablet"), "mobile activity", "other")
        '''
    },
    {
        "id": 260,
        "category": "CASE",
        "title": "Nested caseExpression within eval for detailed evaluation",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval detailedOutcome = case(
                age < 18, "minor",
                age >= 18 AND age <= 65, case(
                    income >= 50000, "adult high income",
                    "adult low income"),
                "senior")
        '''
    },
    {
        "id": 261,
        "category": "CASE",
        "title": "Using caseExpression with boolean logic",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval booleanLogic = case(
                x > 10 OR y < 20, "Condition 1 met",
                z == 5 AND a != b, "Condition 2 met",
                "Default")
        '''
    },
    {
        "id": 262,
        "category": "CASE",
        "title": "Combining NOT with comparisonExpression for exclusion",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval exclusionCheck = if(NOT (x >= 100 AND y <= 200), "within range", "out of range")
        '''
    },
    {
        "id": 263,
        "category": "CONDITIONAL",
        "title": "Dynamically determining status with ifExpression",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval status = if(temperature > 100, "overheated", "normal")
        '''
    },
    {
        "id": 264,
        "category": "CONDITIONAL",
        "title": "Evaluating multiple conditions with inExpression and comparisonExpression",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval complexIn = if((user IN ("user1", "user2") AND score > 50) OR (NOT status IN ("completed", "passed")), "action needed", "ok")
        '''
    },
    {
        "id": 265,
        "category": "CONDITIONAL",
        "title": "Multiple nested conditions with caseExpression and inExpression",
        "complexity": "complex",
        "query": '''
            | table file="477457c9-17d3-4e0b-ab5d-b3d55d906480"
            | eval nestedConditions = case(
                category IN ("A", "B") AND score >= 90, "High in A/B",
                category IN ("C", "D") OR score < 50, "Low or C/D",
                "Default")
        '''
    }
]
final_string = ''

for unit_test_dict in test_queries:
    output_string = ''
    for _key, _val in unit_test_dict.items():
        if _key == 'id':
            output_string += f'- "{_key}": "{_val}"\n'
        elif _key == 'query':
            output_string += f'  "{_key}": |\n'
            _val = _val.lstrip('\n').rstrip('\n')
            for line in _val.split('\n'):
                if line == '\n':
                    output_string += f'      {line}'
                    continue

                if line.startswith('            '):
                    output_string += f'      {line[12:]}\n'

        else:
            output_string += f'  "{_key}": "{_val}"\n'

    print(output_string)
    final_string += f'{output_string}\n'

with open('OLD_unit_tests.yaml', 'w') as file_out:
    file_out.write(final_string)
