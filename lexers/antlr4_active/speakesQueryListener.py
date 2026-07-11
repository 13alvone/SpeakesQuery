# Generated from speakesQuery.g4 by ANTLR 4.13.1
from antlr4 import *
if "." in __name__:
    from .speakesQueryParser import speakesQueryParser
else:
    from speakesQueryParser import speakesQueryParser

# This class defines a complete listener for a parse tree produced by speakesQueryParser.
class speakesQueryListener(ParseTreeListener):

    # Enter a parse tree produced by speakesQueryParser#speakesQuery.
    def enterSpeakesQuery(self, ctx:speakesQueryParser.SpeakesQueryContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#speakesQuery.
    def exitSpeakesQuery(self, ctx:speakesQueryParser.SpeakesQueryContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#initialSequence.
    def enterInitialSequence(self, ctx:speakesQueryParser.InitialSequenceContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#initialSequence.
    def exitInitialSequence(self, ctx:speakesQueryParser.InitialSequenceContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#expression.
    def enterExpression(self, ctx:speakesQueryParser.ExpressionContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#expression.
    def exitExpression(self, ctx:speakesQueryParser.ExpressionContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#conjunction.
    def enterConjunction(self, ctx:speakesQueryParser.ConjunctionContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#conjunction.
    def exitConjunction(self, ctx:speakesQueryParser.ConjunctionContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#comparison.
    def enterComparison(self, ctx:speakesQueryParser.ComparisonContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#comparison.
    def exitComparison(self, ctx:speakesQueryParser.ComparisonContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#additiveExpr.
    def enterAdditiveExpr(self, ctx:speakesQueryParser.AdditiveExprContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#additiveExpr.
    def exitAdditiveExpr(self, ctx:speakesQueryParser.AdditiveExprContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#multiplicativeExpr.
    def enterMultiplicativeExpr(self, ctx:speakesQueryParser.MultiplicativeExprContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#multiplicativeExpr.
    def exitMultiplicativeExpr(self, ctx:speakesQueryParser.MultiplicativeExprContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#unaryExpr.
    def enterUnaryExpr(self, ctx:speakesQueryParser.UnaryExprContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#unaryExpr.
    def exitUnaryExpr(self, ctx:speakesQueryParser.UnaryExprContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#primary.
    def enterPrimary(self, ctx:speakesQueryParser.PrimaryContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#primary.
    def exitPrimary(self, ctx:speakesQueryParser.PrimaryContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#timeClause.
    def enterTimeClause(self, ctx:speakesQueryParser.TimeClauseContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#timeClause.
    def exitTimeClause(self, ctx:speakesQueryParser.TimeClauseContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#earliestClause.
    def enterEarliestClause(self, ctx:speakesQueryParser.EarliestClauseContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#earliestClause.
    def exitEarliestClause(self, ctx:speakesQueryParser.EarliestClauseContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#latestClause.
    def enterLatestClause(self, ctx:speakesQueryParser.LatestClauseContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#latestClause.
    def exitLatestClause(self, ctx:speakesQueryParser.LatestClauseContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#indexClause.
    def enterIndexClause(self, ctx:speakesQueryParser.IndexClauseContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#indexClause.
    def exitIndexClause(self, ctx:speakesQueryParser.IndexClauseContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#comparisonOperator.
    def enterComparisonOperator(self, ctx:speakesQueryParser.ComparisonOperatorContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#comparisonOperator.
    def exitComparisonOperator(self, ctx:speakesQueryParser.ComparisonOperatorContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#inExpression.
    def enterInExpression(self, ctx:speakesQueryParser.InExpressionContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#inExpression.
    def exitInExpression(self, ctx:speakesQueryParser.InExpressionContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#inputlookupInit.
    def enterInputlookupInit(self, ctx:speakesQueryParser.InputlookupInitContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#inputlookupInit.
    def exitInputlookupInit(self, ctx:speakesQueryParser.InputlookupInitContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#loadjobInit.
    def enterLoadjobInit(self, ctx:speakesQueryParser.LoadjobInitContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#loadjobInit.
    def exitLoadjobInit(self, ctx:speakesQueryParser.LoadjobInitContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#makeresultsInit.
    def enterMakeresultsInit(self, ctx:speakesQueryParser.MakeresultsInitContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#makeresultsInit.
    def exitMakeresultsInit(self, ctx:speakesQueryParser.MakeresultsInitContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#makeresultsArg.
    def enterMakeresultsArg(self, ctx:speakesQueryParser.MakeresultsArgContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#makeresultsArg.
    def exitMakeresultsArg(self, ctx:speakesQueryParser.MakeresultsArgContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#validLine.
    def enterValidLine(self, ctx:speakesQueryParser.ValidLineContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#validLine.
    def exitValidLine(self, ctx:speakesQueryParser.ValidLineContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#directive.
    def enterDirective(self, ctx:speakesQueryParser.DirectiveContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#directive.
    def exitDirective(self, ctx:speakesQueryParser.DirectiveContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#macro.
    def enterMacro(self, ctx:speakesQueryParser.MacroContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#macro.
    def exitMacro(self, ctx:speakesQueryParser.MacroContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#statsAgg.
    def enterStatsAgg(self, ctx:speakesQueryParser.StatsAggContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#statsAgg.
    def exitStatsAgg(self, ctx:speakesQueryParser.StatsAggContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#variableList.
    def enterVariableList(self, ctx:speakesQueryParser.VariableListContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#variableList.
    def exitVariableList(self, ctx:speakesQueryParser.VariableListContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#subsearch.
    def enterSubsearch(self, ctx:speakesQueryParser.SubsearchContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#subsearch.
    def exitSubsearch(self, ctx:speakesQueryParser.SubsearchContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#subsearchContent.
    def enterSubsearchContent(self, ctx:speakesQueryParser.SubsearchContentContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#subsearchContent.
    def exitSubsearchContent(self, ctx:speakesQueryParser.SubsearchContentContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#functionCall.
    def enterFunctionCall(self, ctx:speakesQueryParser.FunctionCallContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#functionCall.
    def exitFunctionCall(self, ctx:speakesQueryParser.FunctionCallContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#numericFunctionCall.
    def enterNumericFunctionCall(self, ctx:speakesQueryParser.NumericFunctionCallContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#numericFunctionCall.
    def exitNumericFunctionCall(self, ctx:speakesQueryParser.NumericFunctionCallContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#stringFunctionCall.
    def enterStringFunctionCall(self, ctx:speakesQueryParser.StringFunctionCallContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#stringFunctionCall.
    def exitStringFunctionCall(self, ctx:speakesQueryParser.StringFunctionCallContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#specificFunctionCall.
    def enterSpecificFunctionCall(self, ctx:speakesQueryParser.SpecificFunctionCallContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#specificFunctionCall.
    def exitSpecificFunctionCall(self, ctx:speakesQueryParser.SpecificFunctionCallContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#statsFunctionCall.
    def enterStatsFunctionCall(self, ctx:speakesQueryParser.StatsFunctionCallContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#statsFunctionCall.
    def exitStatsFunctionCall(self, ctx:speakesQueryParser.StatsFunctionCallContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#regexTarget.
    def enterRegexTarget(self, ctx:speakesQueryParser.RegexTargetContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#regexTarget.
    def exitRegexTarget(self, ctx:speakesQueryParser.RegexTargetContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#mvfindObject.
    def enterMvfindObject(self, ctx:speakesQueryParser.MvfindObjectContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#mvfindObject.
    def exitMvfindObject(self, ctx:speakesQueryParser.MvfindObjectContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#mvindexIndex.
    def enterMvindexIndex(self, ctx:speakesQueryParser.MvindexIndexContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#mvindexIndex.
    def exitMvindexIndex(self, ctx:speakesQueryParser.MvindexIndexContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#mvDelim.
    def enterMvDelim(self, ctx:speakesQueryParser.MvDelimContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#mvDelim.
    def exitMvDelim(self, ctx:speakesQueryParser.MvDelimContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#timespan.
    def enterTimespan(self, ctx:speakesQueryParser.TimespanContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#timespan.
    def exitTimespan(self, ctx:speakesQueryParser.TimespanContext):
        pass


    # Enter a parse tree produced by speakesQueryParser#variableName.
    def enterVariableName(self, ctx:speakesQueryParser.VariableNameContext):
        pass

    # Exit a parse tree produced by speakesQueryParser#variableName.
    def exitVariableName(self, ctx:speakesQueryParser.VariableNameContext):
        pass



del speakesQueryParser