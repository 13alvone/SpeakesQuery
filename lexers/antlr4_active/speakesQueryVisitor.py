# Generated from speakesQuery.g4 by ANTLR 4.13.1
from antlr4 import *
if "." in __name__:
    from .speakesQueryParser import speakesQueryParser
else:
    from speakesQueryParser import speakesQueryParser

# This class defines a complete generic visitor for a parse tree produced by speakesQueryParser.

class speakesQueryVisitor(ParseTreeVisitor):

    # Visit a parse tree produced by speakesQueryParser#speakesQuery.
    def visitSpeakesQuery(self, ctx:speakesQueryParser.SpeakesQueryContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#initialSequence.
    def visitInitialSequence(self, ctx:speakesQueryParser.InitialSequenceContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#expression.
    def visitExpression(self, ctx:speakesQueryParser.ExpressionContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#conjunction.
    def visitConjunction(self, ctx:speakesQueryParser.ConjunctionContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#comparison.
    def visitComparison(self, ctx:speakesQueryParser.ComparisonContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#additiveExpr.
    def visitAdditiveExpr(self, ctx:speakesQueryParser.AdditiveExprContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#multiplicativeExpr.
    def visitMultiplicativeExpr(self, ctx:speakesQueryParser.MultiplicativeExprContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#unaryExpr.
    def visitUnaryExpr(self, ctx:speakesQueryParser.UnaryExprContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#primary.
    def visitPrimary(self, ctx:speakesQueryParser.PrimaryContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#timeClause.
    def visitTimeClause(self, ctx:speakesQueryParser.TimeClauseContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#earliestClause.
    def visitEarliestClause(self, ctx:speakesQueryParser.EarliestClauseContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#latestClause.
    def visitLatestClause(self, ctx:speakesQueryParser.LatestClauseContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#indexClause.
    def visitIndexClause(self, ctx:speakesQueryParser.IndexClauseContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#comparisonOperator.
    def visitComparisonOperator(self, ctx:speakesQueryParser.ComparisonOperatorContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#inExpression.
    def visitInExpression(self, ctx:speakesQueryParser.InExpressionContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#inputlookupInit.
    def visitInputlookupInit(self, ctx:speakesQueryParser.InputlookupInitContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#loadjobInit.
    def visitLoadjobInit(self, ctx:speakesQueryParser.LoadjobInitContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#makeresultsInit.
    def visitMakeresultsInit(self, ctx:speakesQueryParser.MakeresultsInitContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#makeresultsArg.
    def visitMakeresultsArg(self, ctx:speakesQueryParser.MakeresultsArgContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#validLine.
    def visitValidLine(self, ctx:speakesQueryParser.ValidLineContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#directive.
    def visitDirective(self, ctx:speakesQueryParser.DirectiveContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#macro.
    def visitMacro(self, ctx:speakesQueryParser.MacroContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#statsAgg.
    def visitStatsAgg(self, ctx:speakesQueryParser.StatsAggContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#variableList.
    def visitVariableList(self, ctx:speakesQueryParser.VariableListContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#subsearch.
    def visitSubsearch(self, ctx:speakesQueryParser.SubsearchContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#subsearchContent.
    def visitSubsearchContent(self, ctx:speakesQueryParser.SubsearchContentContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#functionCall.
    def visitFunctionCall(self, ctx:speakesQueryParser.FunctionCallContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#numericFunctionCall.
    def visitNumericFunctionCall(self, ctx:speakesQueryParser.NumericFunctionCallContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#stringFunctionCall.
    def visitStringFunctionCall(self, ctx:speakesQueryParser.StringFunctionCallContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#specificFunctionCall.
    def visitSpecificFunctionCall(self, ctx:speakesQueryParser.SpecificFunctionCallContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#statsFunctionCall.
    def visitStatsFunctionCall(self, ctx:speakesQueryParser.StatsFunctionCallContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#regexTarget.
    def visitRegexTarget(self, ctx:speakesQueryParser.RegexTargetContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#mvfindObject.
    def visitMvfindObject(self, ctx:speakesQueryParser.MvfindObjectContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#mvindexIndex.
    def visitMvindexIndex(self, ctx:speakesQueryParser.MvindexIndexContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#mvDelim.
    def visitMvDelim(self, ctx:speakesQueryParser.MvDelimContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#timespan.
    def visitTimespan(self, ctx:speakesQueryParser.TimespanContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by speakesQueryParser#variableName.
    def visitVariableName(self, ctx:speakesQueryParser.VariableNameContext):
        return self.visitChildren(ctx)



del speakesQueryParser