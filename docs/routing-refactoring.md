Refactoring routing
===================

Current routing rule uses two custom stanza attributes:
 * `origin`
 * `destination`

The actuals `from` and `to` attributes are replaced "at the right time" with
`origin` and `destination` so the stanza will be delivered to the correct
component.
However, this rule can lead to confusion, bad code mainteinance and difficulties
in debugging and tracking stanza errors.


 * TODO direct component addressing (e.g. `resolver.prime.kontalk.net`)
 * TODO envelop forwarding