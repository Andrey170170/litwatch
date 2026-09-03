from litwatch.models import Confidence, Flags, ScreenOutput, Tier, derive_tier


def test_tier_derivation():
    assert derive_tier(Flags(cot=True, internals=True, mech_interp=True)) == Tier.MUST_READ
    assert derive_tier(Flags(cot=True, internals=True, mech_interp=False)) == Tier.MUST_READ
    assert derive_tier(Flags(cot=True, internals=False, mech_interp=False)) == Tier.AWARE
    assert derive_tier(Flags(cot=False, internals=True, mech_interp=True)) == Tier.MECH_INTERP
    # Internals without a mech-interp framing is not tier 3: probes for calibration, say.
    assert derive_tier(Flags(cot=False, internals=True, mech_interp=False)) == Tier.NONE
    assert derive_tier(Flags(cot=False, internals=False, mech_interp=False)) == Tier.NONE


def test_screen_output_maps_to_flags():
    out = ScreenOutput(
        is_cot_monitoring_or_faithfulness=True,
        uses_internals=False,
        is_mech_interp=False,
        confidence=Confidence.HIGH,
        reason="x",
    )
    assert out.flags == Flags(cot=True, internals=False, mech_interp=False)
    assert out.tier == Tier.AWARE
