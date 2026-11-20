// lk_speech_version() and compile-time checks on the public header (PRD §6.5: the C ABI).
//
// The version is written in C so the string names the macOS SDK this library was built against,
// read from the SDK's own availability macro at compile time.

#include <AvailabilityMacros.h>

#include "../../include/lookout_speech.h"

#define LK_STR2(x) #x
#define LK_STR(x) LK_STR2(x)

// Bump with any change to behaviour or the ABI. 0.2.0: LK_EV_STABLE (additive).
#define LK_SPEECH_LIBRARY_VERSION "0.2.0"

_Static_assert(LK_EV_READY == 0 && LK_EV_PARTIAL == 1 && LK_EV_FINAL == 2 && LK_EV_ERROR == 3 &&
                   LK_EV_STABLE == 4,
               "the event kinds are part of the ABI");
_Static_assert(sizeof(lk_event_kind) == 4, "lk_event_kind is passed as a 32-bit integer");
_Static_assert(LK_SPEECH_OK == 0 && LK_SPEECH_ASSET_NOT_INSTALLED == 1 &&
                   LK_SPEECH_NOT_AUTHORIZED == 2 && LK_SPEECH_LOCALE_UNSUPPORTED == 3,
               "the status codes are part of the ABI");

const char *lk_speech_version(void) {
    return "lookout-speech " LK_SPEECH_LIBRARY_VERSION
           " (SpeechAnalyzer + DictationTranscriber, on device only; macOS SDK "
           LK_STR(__MAC_OS_X_VERSION_MAX_ALLOWED) ")";
}
