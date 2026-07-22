import { Lexer } from "marked"
import { atomizeResponseClaimsWithLexer } from "./claim-atomization-core"

export type { AtomizedResponseClaim, ClaimAtomizationStatus } from "./claim-atomization-core"

export function atomizeResponseClaims(input: unknown) {
  return atomizeResponseClaimsWithLexer(input, (source, options) => Lexer.lex(source, options))
}
