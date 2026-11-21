// sg_speech_version() and compile-time checks on the public header (PRD §6.5: the C ABI).
//
// The version is written in C so the string names the macOS SDK this library was built against,
// read from the SDK's own availability macro at compile time.

#include <AvailabilityMacros.h>

#include "../../include/snow_globe_speech.h"

#define SG_STR2(x) #x
#define SG_STR(x) SG_STR2(x)

// Bump with any change to behaviour or the ABI. 0.2.0: the STABLE event (additive). 0.3.0: the
// rename from Lookout to Snow Globe (D-080): every C name's prefix is now sg_/SG_ (was lk_/LK_),
// the header is snow_globe_speech.h and the archive libsnow_globe_speech.a; the ten functions,
// their signatures, the constants' values and the behaviour are unchanged.
#define SG_SPEECH_LIBRARY_VERSION "0.3.0"

_Static_assert(SG_EV_READY == 0 && SG_EV_PARTIAL == 1 && SG_EV_FINAL == 2 && SG_EV_ERROR == 3 &&
                   SG_EV_STABLE == 4,
               "the event kinds are part of the ABI");
_Static_assert(sizeof(sg_event_kind) == 4, "sg_event_kind is passed as a 32-bit integer");
_Static_assert(SG_SPEECH_OK == 0 && SG_SPEECH_ASSET_NOT_INSTALLED == 1 &&
                   SG_SPEECH_NOT_AUTHORIZED == 2 && SG_SPEECH_LOCALE_UNSUPPORTED == 3,
               "the status codes are part of the ABI");

const char *sg_speech_version(void) {
    return "snow-globe-speech " SG_SPEECH_LIBRARY_VERSION
           " (SpeechAnalyzer + DictationTranscriber, on device only; macOS SDK "
           SG_STR(__MAC_OS_X_VERSION_MAX_ALLOWED) ")";
}
