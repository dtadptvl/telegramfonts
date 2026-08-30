# AIxAI AGENT PROTOCOL v1.7

## ROLE
prime=orchestrate,route,stateAuthority,merge
sub=executeRequestedScopeOnly

## OBJECTIVE
priority=semanticFidelity>interop>taskCost>tokenCost
objective=minExpectedTaskCostWithoutBehaviorChange
taskCost=txTokens+retryCost+parseFailCost+latencyCost
optimize=expectedTaskCost
compressionStop=expectedRetryCost>=expectedTokenSaving

## GENERAL
omit=articles,polite,prose,repeat,transition,reasonNarr,inferableCtx
prefer=shortUnambigASCII,stableAbbr,stableID,explicitKey,closedEnum
avoid=markdown,json,rareUnicode,modelSpecificAbbr,deepInference
semanticFidelity>compression if ambiguityOrLossCanChangeBehavior
sendOnly=infoNeededForNextAction
include=requiredConstraint,stateRef,stateVer,materialUnknown
repeatConcept=>stableID
dropCtxOnlyIfSharedStateConfirmed=1
unknown=>explicit
reasonNarr=off unless verificationRequiresEvidence
doNotFabricateMissingValue=1
noChainOfThought=1

## ARCHITECTURE
baseProtocol=Q,R,X
optionalModules=STATE1,SCHEMA1,PROG1,CODE1
controlPlane=DSL1
largeSemanticPayload=BODY1
primeLoadsOnlyRequiredModules=1
disposableSubAssumeBaseOnlyUnlessBootstrapSaysOtherwise=1

## PROTOCOL
pv=1.7
cap=DSL1
encoding=UTF8
runtimeFormat=keyValue
positionalDefault=off

## WIRE_GRAMMAR
record=T|field;field
field=key=value
recordSep=LF
typeSep=PIPE
fieldSep=SEMICOLON
itemSep=COMMA
assign=EQUAL
escapePrefix=BACKSLASH

## CHAR_CODES
BACKSLASH=0x5C
PIPE=0x7C
SEMICOLON=0x3B
COMMA=0x2C
EQUAL=0x3D
LF=0x0A

## ESCAPE
literalBackslash=\\
literalPipe=\|
literalSemicolon=\;
literalComma=\,
literalEqual=\=
literalLF=\n
literalEmptyMarker=\_
literalNullMarker=\~
unknownEscape=>X|code=PARSE

## SPECIAL_VALUES
empty=_
null=~
boolFalse=0
boolTrue=1
boolInterpretation=onlyIfFieldTypeBool
emptyAllowed=onlyIfSchemaOrTaskAllows
nullAllowed=onlyIfSchemaOrTaskAllows
literalUnderscore=\_
literalTilde=\~

## PARSE
parseMode=incrementalEscapeAware
parseOrder=records>firstUnescapedTypeSep>fieldsLeftToRight>firstUnescapedAssign>itemsIfList>recognizeSpecialMarkerOnRawItem>unescape
fieldScan=escapeAware
bodyKeySpecial=body
bodyMustBeLast=1
whenBodyKeySeen=consumeRemainderAsOpaqueValue
bodyValueDoesNotSplitOn=PIPE,SEMICOLON,COMMA,EQUAL
bodyEscapes=BACKSLASH,LF
bodyUnknownEscape=>X|code=PARSE
strictWireRequiresSerializer=1
promptNativeBodyParseFailure=>fallbackExplicitEN
listSplit=escapeAwareCommaBeforeUnescape
builtInListFields=mods,use,out,req,sd
specialMarkerRecognition=wholeRawScalarOrListItemBeforeUnescape
escapedMarkerIsLiteral=1
literalLFEncodedAs=\n
parseFail=>X|code=PARSE
recordHasExactlyOneType=1

## BODY1
purpose=largeOrSeparatorHeavySemanticPayload
field=body
bodyMustBeLast=1
bodyIsOpaqueUTF8String=1
bodyNoListParsing=1
bodyNoSpecialEmptyOrNullMarkers=1
bodyEscapeOnly=BACKSLASH,LF
bodyUseWhen=code,sql,diff,log,longText,json,markdown,separatorHeavyPayload
bodyAvoidWhen=smallStructuredScalarResult
BODY1IsBaseCompatible=1

## RUNTIME_TYPES_BASE
Q=request
R=result
X=error
terminalTypes=R,X
oneTerminalResponsePerTx=1
omitAbsentTypes=1

## OPTIONAL_MODULE_TYPES
STATE1=ST,DL
PROG1=U
SCHEMA1=noNewRecordType
CODE1=noNewRecordType

## TYPE_RULES
unknownTypeNotActivated=>X|code=TYPE;type=_
U=nonterminalOnly
ST=stateSnapshotOrReference
DL=authoritativeStateMutation
DLMayAccompanyTerminal=1
multiRecordOrder=Q,ST,U,DL,X,R
moduleActivationMustPrecedeModuleRecord=1
QWithUseMustPrecedeFirstModuleRecord=1

## REQUEST
structuredRequestRequires=op
textRequestRequires=task
requestRequiresOneOf=op,task
op=closedEnumUnlessSchemaMarksOpen
coreOps=hello
task=shortExplicitEN
out=commaListOfRequestedFields
req=commaListOfRequiredExtensionKeys
use=commaListOfRequiredModules
coreRequiredFieldsNeedNotAppearInReq=1
noUnrequestedExpansion=1
bodyAllowedForRequest=1

## RESPONSE
R=machineConsumableResult
fallbackText=R|text=shortExplicitEN
emitOnly=requiredProtocolFields,requestedFields
bodyAllowedForResult=1
doNotEmit=unrequestedHypothesis,unrequestedEvidence,unrequestedDecision
minimalSufficient=1
successStopWhenNextActionPossible=1
materialUnknownBlockingCompletion=>X|code=AMBIG;need=*
materialFailureBlockingCompletion=>XWithMatchingErrorCode
materialUnknownNonblocking=>emitOnlyIfRequestedOrSchemaPermits
verificationNeeded=>evidenceSummaryOnlyIfRequestedOrRequired

## FIELD_ORDER
coreOrder=pv,cap,mods,tx,use,op,task,state,sr,sv,sd,out,req,status,code,need,have
schemaFieldsAfterCore=1
schemaFieldOrder=schemaDefined
extraFieldOrder=lexicalUnlessTaskRequiresOrder
bodyAlwaysLast=1

## VERSION
protocolVersion=1.7
protoField=pv
capabilityField=cap
capability=DSL1
bootstrapMayPredeclare=pv,cap,mods
handshakeRequiredOnlyIfCapabilityUnknown=1
handshakeSkipIfBootstrapConfirmed=1
handshakeRequest=Q|op=hello;pv=1.7;cap=DSL1
handshakeResponse=R|status=ok;pv=1.7;cap=DSL1;mods=SUPPORTED_MODULES
majorMismatch=>X|code=PROTO;have=*;need=*
minorMismatch=>bestEffort
versionSend=handshake,change,recovery,mismatch
versionOmitOnlyIfSharedVersionConfirmed=1
wireGrammarStableWithinMajor=1
unknownOptionalFieldAcrossMinor=>ignore
unknownRequiredFieldAcrossMinor=>X|code=UNSUP;item=*

## MODULE_NEGOTIATION
mods=commaListOfSupportedModules
use=commaListOfModulesRequiredForTx
baseNeedsNoUseField=1
unknownRequiredModule=>X|code=UNSUP;item=*
moduleFieldWithoutUse=>X|code=UNSUP;item=REQUIRED_MODULE
moduleKernelLoadedOnlyIfUsed=1
primeMustNotSendUnusedModuleKernel=1
moduleSemanticsStableWithinModuleMajor=1

## BOOTSTRAP
bootstrapOncePerAgentContext=1
bootstrapPreferredForDisposableAgent=1
doNotRepeatFullProtocolIfBootstrapConfirmed=1
disposableAgentAssumeNoPriorStateUnlessExplicitRef=1
neverAssumeHiddenStateSync=1
primeShouldSendMinimalRuntimeKernel=1
bootstrapMayIncludeSupportedModules=1
bootstrapMayIncludeSharedSchemaResolver=1
bootstrapMayIncludeSharedStateResolver=1

## KEY
keyFormat=ASCIIalnumUnderscore
keyFirst=alpha
keyMaxLen=32
keyRegex=^[A-Za-z][A-Za-z0-9_]{0,31}$

## ID
ID=stableWithinTask
IDFormat=ASCIIalnumUnderscore
IDFirst=alpha
IDMaxLen=16
IDRegex=^[A-Za-z][A-Za-z0-9_]{0,15}$
sameEntity=>sameID
collision=>X|code=IDCOL;id=_

## TX
tx=transactionCorrelationID
txFormat=IDFormat
txRequiredIfConcurrentOrMultiplexed=1
txRequiredForParallelAgents=1
txOptionalIfExactlyOneOutstandingTxOnChannel=1
replyPreserveTx=1
txUniqueWithinActiveScope=1

## STATE1
stateModule=optional
stateModuleRequiredIfStatePersistsAcrossTurnsOrAgents=1
stateMustHave=id,ver
stateRef=@id:ver
stateRecord=ST|state=id;ver=ver
primeIsStateAuthority=1
subMustNotInventStateVersion=1
stateUpdateValidOnlyFromDeclaredAuthority=1
authoritativeMutation=DL|state=id;from=ver;to=ver;chg=value
largeMutationPayload=>DL|state=id;from=ver;to=ver;body=OPAQUE
subProposedMutation=>returnAsRequestedResultField
stateMismatch=>X|code=STATE;have=*;need=*
stateRecovery=requestMissingStateOrExplicitFallback
relayState=>preserveIDAndVersion
staleResult=>X|code=STATE;have=*;need=*

## SCHEMA1
schemaModule=optional
schemaVersionField=sv
schemaRefField=sr
schemaRef=@schemaID:ver
schemaDescriptorField=sd
keyValueSchema=default
positionalDefault=off
positionalAllowedOnlyIfSchemaVersionShared=1
schemaRequiredForPositional=1
schemaResolutionPreference=sharedRef>minimalInlineDescriptor
sharedSchemaAvailable=>sendSrOnly
sharedSchemaUnavailable=>sendMinimalSd
unresolvedSchemaRef=>X|code=SCHEMA;need=sr
missingSchemaForPositional=>X|code=SCHEMA;need=sv
schemaMismatch=>X|code=SCHEMA;have=*;need=*
srVersionMustEqualSvIfBothPresent=1
schemaDefines=fieldType,required,enum,listSemantics,outputOrder
schemaOverridesGenericFieldTyping=1
sd=commaListOfFieldDecl
fieldDecl=name:type:mode
fieldDeclMode=req,opt
fieldDeclTypes=str,int,float,bool,id,ref,list(TYPE),enum(A/B/C)
fieldDeclNameAndEnumToken=IDFormat
sdDeclarationOrder=outputOrderUnlessExplicitlyOverridden
sdNoNestedObjectGrammar=1
complexInlineSchema=>preferSrOrExplicitEN
sdMustContainOnlySemanticsNeededForCurrentTx=1

## TYPES
scalarDefault=UTF8String
typedValueOnlyIfSchemaDeclaresType=1
listOnlyIfSchemaOrFieldSemanticsDeclareList=1
nullAllowedOnlyIfSchemaOrTaskAllows=1
emptyAllowedOnlyIfSchemaOrTaskAllows=1

## ENUM
enum=closedUnlessOpen
unknownEnum=>X|code=ENUM;key=*;val=*

## REQUIRED_FIELDS
requiredMissing=>X|code=MISS;field=_
neverInferRequiredUnknownValue=1
missingValue=>explicitUnknownOrMISS

## EXTENSIONS
knownCoreFields=pv,cap,mods,use,tx,op,task,state,sr,sv,sd,out,req,status,code,need,have,text,body
unknownFieldDefault=optionalExtension
unknownOptionalExtension=>ignore
unknownRequiredExtension=>X|code=UNSUP;item=_
criticalExtensionMustAppearInReq=1
requiredExtensionKeysAreSemanticConstraints=1
futureMinorFieldWithoutReq=>ignore
futureMinorFieldWithReq=>UNSUPIfUnknown
stateFieldRequiresUse=STATE1
srSvSdFieldsRequireUse=SCHEMA1

## AMBIGUITY
implicitInferenceAllowedOnlyIfUniqueAndLowRisk=1
materialAmbiguity=>X|code=AMBIG;need=_
doNotGuessRequiredUnknown=1

## CONSTRAINTS
latestExplicitMutableValueOverridesEarlierSameScope=1
immutableConstraintCannotBeOverriddenWithoutExplicitSupersede=1
constraintConflict=>X|code=CONFLICT;a=*;b=*
priorityWinsOnlyIfPriorityExplicit=1
otherwiseConflict=>X|code=CONFLICT;a=*;b=*

## CODE1
codebook=optional
codebookDefault=off
codebookUseOnlyIfExpectedReuseSaving>definitionCost+riskCost
codebookEnableOnlyIfSyntaxSharedInBootstrapOrSchema=1
undefinedSymbol=>X|code=UNDEF;raw=_
newSymbol=>defineBeforeUse
codebookAvoidForDisposableAgentUnlessHighReuse=1

## PROG1
progressModule=optional
progressType=U
progressUseOnlyIfConsumerCanActBeforeTerminal=1
progressAvoidForShortDisposableTasks=1
progressMustPreserveTx=1
progressNonterminal=1
canonicalProgress=U|tx=t1;status=VALUE

## COMPRESSION
compression=losslessByDefault
choose=minRobustTokenCost
minRobustTokenCost=minimizeWorstExpectedTokenizerCostAcrossAgents
tokenizerUnknown=>preferShortASCII
tokenCostUnknown=>robustness>characterCount
abbrevAllowedOnlyIfSharedOrDefined=1
implicitInferenceAllowedOnlyIfUniqueAndLowRisk=1
stopCompressionWhenExpectedRetryCost>=ExpectedTokenSaving
doNotCompress=IDs,stateRef,stateVer,errorCode,requiredConstraint,materialUnknown
largeSeparatorHeavyPayload=>preferBodyOverEscapedFields

## LANGUAGE_SELECTION
DSLUse=structured,deterministic,lowAmbiguityTasks
explicitENUse=semanticComplexityHighOrDSLAddsAmbiguity
fallback=shortExplicitEN
fallbackTrigger=ambiguity,parseRisk,stateRisk,unsupportedEncoding
fallbackPreserve=tx,IDs,constraints,stateVersion,decision,unknown
fallbackStillUsesRecordType=1
longFallback=>body

## PRIME_POLICY
primeRole=orchestrate,route,stateAuthority,merge
primeOwns=globalObjective,taskGraph,stateVersion,mergePolicy,moduleRegistry,schemaRegistry
primeMustNotSendFullMasterSpecPerTransaction=1
primeSendToSub=minKernel+requiredModuleKernels+task+requiredConstraints+stateRef+schemaRefOrMinimalSchema+outputContract
primeDoNotSend=costTheory,unusedRules,unusedModules,unusedSchemas,unrelatedContext
primeMustNotAssumeCrossAgentContext=1
primeMustPreserveSemanticConstraints=1
primeMustPreserveIDs=1
primeMustPreserveStateVersion=1
primeMustPreserveTx=1
primeMergeBy=tx,ID,stateVer
mergeConflict=>X|code=CONFLICT;a=*;b=*
primeRejectStaleResult=1
primePreferReferenceOverRepeatedPayloadWhenResolverConfirmed=1

## SUB_POLICY
subRole=executeRequestedScopeOnly
subNoUnrequestedExpansion=1
subReturn=minimalSufficient
subMustExposeMaterialUnknown=1
subMustExposeMaterialFailure=1
subMustNotExposeChainOfThought=1
subVerificationNeeded=>evidenceSummaryOnly
subMustNotInventMissingState=1
subMustNotInventRequiredValue=1
subMustNotChangeObjective=1
subMustNotChangeConstraints=1
subMustNotAssumeModuleLoadedUnlessBootstrapOrUseConfirms=1
subMustNotAssumeSchemaResolvableUnlessBootstrapConfirms=1

## PARALLEL
parallelAgents=>txRequired
parallelAgentsDoNotShareStateUnlessExplicitStateRef=1
agentOutputIndependentlyParseable=1
primeMustNotAssumeCrossAgentContext=1
primeMergeBy=tx,ID,stateVer
staleParallelResult=>X|code=STATE;have=*;need=*

## RELAY
relayTx=>preserve
relayState=>preserveIDAndVersion
relayConstraint=>preserveUnlessExplicitlySuperseded
relayRequiredUnknownField=>preserveIfForwardingToCapableAgent
relayOptionalUnknownField=>mayDrop
transparentRelayUnknownType=>forwardBytesUnchangedOnlyIfRelayDoesNotExecuteOrParse
executingAgentUnknownType=>X|code=TYPE;type=_
relayMachineFields=>doNotRephraseUnlessRequired
relayDoNotInferMissingContext=1

## ERRORS
errorMinimal=code+fieldsNeededForRecovery
errorNoReasonNarr=1
retryOnlyIfErrorRecoverable=1
retryMustChange=input,state,schema,capability,moduleOrConstraint
identicalRetryAvoid=1

## ERROR_CODES
PROTO=protocolVersionMismatch
TYPE=unknownOrInactiveRecordType
PARSE=wireParseFailure
MISS=requiredFieldMissing
ENUM=unknownClosedEnumValue
UNSUP=unsupportedRequiredFeatureOrModule
STATE=stateVersionMismatch
SCHEMA=schemaMismatchMissingOrUnresolved
IDCOL=idCollision
CONFLICT=constraintOrMergeConflict
UNDEF=undefinedCodebookSymbol
AMBIG=materialAmbiguity

## OUTPUT
output=minimalSufficient
noRestateInput=1
noMetaCommentary=1
noPolitenessTokens=1
noChainOfThought=1
noHiddenAssumption=1
verificationNeeded=>evidenceSummaryOnly
successStopWhenNextActionPossible=1

## RUNTIME_KERNEL_BASE_FOR_DISPOSABLE_SUB
pv=1.7
cap=DSL1
fmt=T|k=v;k=v
baseTypes=Q,R,X
sep=PIPE,SEMICOLON,COMMA,EQUAL
escape=BACKSLASH
escBS=\\
escPipe=\|
escSemi=\;
escComma=\,
escEq=\=
escLF=\n
escEmpty=\_
escNull=\~
empty=_
null=~
split=escapeAware
specialMarkerBeforeUnescape=1
body=opaqueLastField
bodyEscape=BACKSLASH,LF
strictWireRequiresSerializer=1
promptNativeBodyParseFailure=>fallbackExplicitEN
QRequires=task|op
out=requestedFieldList
replyPreserveTx=1
terminal=R,X
oneTerminalPerTx=1
parseFail=>X|code=PARSE
unknownRequired=>X|code=UNSUP
missingRequired=>X|code=MISS
materialAmbiguity=>X|code=AMBIG
noInferRequiredUnknown=1
noUnrequestedExpansion=1
output=minimalSufficient
noChainOfThought=1

## MODULE_KERNEL_STATE1
module=STATE1
activateBeforeSTDL=Q.use
stateRef=@id:ver
primeIsAuthority=1
subNoInventVersion=1
mismatch=>X|code=STATE;have=*;need=*

## MODULE_KERNEL_SCHEMA1
module=SCHEMA1
sr=@schemaID:ver
sd=name:type:mode,...
mode=req|opt
prefer=sharedRef>minimalInlineDescriptor
unresolved=>X|code=SCHEMA;need=sr

## MODULE_KERNEL_PROG1
module=PROG1
type=U
nonterminal=1
useOnlyIfActionable=1

## MODULE_KERNEL_CODE1
module=CODE1
undefined=>X|code=UNDEF;raw=_
defineBeforeUse=1

## CANONICAL_HANDSHAKE
Q|op=hello;pv=1.7;cap=DSL1
R|status=ok;pv=1.7;cap=DSL1;mods=STATE1,SCHEMA1,PROG1

## CANONICAL_REQUEST_TEXT
Q|tx=t1;task=SHORT_EXPLICIT_EN;out=FIELD1,FIELD2

## CANONICAL_REQUEST_STRUCTURED
Q|tx=t1;op=OP;out=FIELD1,FIELD2

## CANONICAL_REQUEST_REQUIRED_EXTENSION
Q|tx=t1;op=OP;out=FIELD1;req=CRITICAL_EXT;CRITICAL_EXT=VALUE

## CANONICAL_REQUEST_WITH_STATE
Q|tx=t1;use=STATE1;task=SHORT_EXPLICIT_EN;state=@s1:7;out=patch

## CANONICAL_REQUEST_WITH_SCHEMA_REF
Q|tx=t1;use=SCHEMA1;op=OP;sr=@extract:3;out=FIELD1,FIELD2

## CANONICAL_REQUEST_WITH_INLINE_SCHEMA
Q|tx=t1;use=SCHEMA1;op=OP;sv=3;sd=FIELD1:str:req,FIELD2:int:opt;out=FIELD1,FIELD2

## CANONICAL_RESULT
R|tx=t1;FIELD1=VALUE;FIELD2=VALUE

## CANONICAL_TEXT_FALLBACK
R|tx=t1;text=SHORT_EXPLICIT_EN

## CANONICAL_BODY_RESULT
R|tx=t1;status=ok;body=SELECT a,b FROM t WHERE x=1; y=2 | keep separators raw

## CANONICAL_PROGRESS
U|tx=t1;status=VALUE

## CANONICAL_STATE_ERROR
X|tx=t1;code=STATE;have=6;need=7

## CANONICAL_MISSING_ERROR
X|tx=t1;code=MISS;field=FIELD

## CANONICAL_AMBIGUITY_ERROR
X|tx=t1;code=AMBIG;need=MISSING_INFO

## CANONICAL_UNSUPPORTED_ERROR
X|tx=t1;code=UNSUP;item=FEATURE

## CANONICAL_SCHEMA_ERROR
X|tx=t1;code=SCHEMA;need=sr

## CANONICAL_STATE_REFERENCE
ST|state=s1;ver=7

## CANONICAL_PRIME_STATE_MUTATION
DL|state=s1;from=7;to=8;chg=VALUE

## ESCAPE_EXAMPLES
raw=A;B
wire=A\;B

raw=A,B
wire=A\,B

raw=A=B
wire=A\=B

raw=A|B
wire=A\|B

raw=A\B
wire=A\\B

raw=A LF B
wire=A\nB

raw=_
wire=\_

raw=~
wire=\~

## BODY_EXAMPLE
raw=if (a==b) { x=1; y=2; } | no field escaping needed
wire=R|tx=t1;body=if (a==b) { x=1; y=2; } | no field escaping needed

## DEPLOYMENT_RULE
masterSpecLivesAtPrimeOrSharedSystemLayer=1
runtimeBaseKernelLivesAtDisposableSubBootstrap=1
moduleKernelSentOnlyWhenNeeded=1
schemaReferencePreferredWhenSharedResolverConfirmed=1
minimalInlineSchemaSentOnlyWhenReferenceUnavailable=1
taskPayloadContainsOnlyNextActionInformation=1
fullMasterSpecMustNotBeRelayedPerTask=1
controlPlaneUsesDSL1=1
largeSemanticPayloadUsesBody=1

## IMPLEMENTATION_INVARIANTS
parserMustBeDeterministic=1
serializerMustRoundTripEscapedScalar=1
serializerMustRoundTripBody=1
serializerMustEscapeEveryBodyBackslash=1
unknownOptionalFieldMustNotChangeBehavior=1
requiredUnknownMustFailClosed=1
stateVersionMustNeverBeGuessed=1
primeMustRejectStaleStateResult=1
sameTxMustCorrelateReply=1
bodyMustAlwaysBeLast=1
baseAgentMustOperateWithoutOptionalModules=1

## RECOMMENDED_CONFORMANCE_TESTS
T1=escapedSemicolonRoundTrip
T2=escapedCommaRoundTrip
T3=escapedEqualRoundTrip
T4=escapedPipeRoundTrip
T5=escapedBackslashRoundTrip
T6=literalEmptyMarkerRoundTrip
T7=literalNullMarkerRoundTrip
T8=bodySeparatorHeavyRoundTrip
T9=unknownOptionalFieldIgnored
T10=unknownRequiredFieldFailsUNSUP
T11=unknownRequiredModuleFailsUNSUP
T12=stateMismatchFailsSTATE
T13=unresolvedSchemaRefFailsSCHEMA
T14=parallelReplyPreservesTx
T15=staleParallelResultRejected
T16=oneTerminalResponsePerTx
T17=escapedSpecialMarkerNotTreatedAsNullOrEmpty
T18=bodyLiteralBackslashRoundTrip
T19=moduleFieldWithoutUseFailsUNSUP
T20=srSvVersionMismatchFailsSCHEMA
