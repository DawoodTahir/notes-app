/**
 * The API base has to be configurable: 127.0.0.1 means "this phone" on a
 * device, so a simulator, a phone on your Wi-Fi, and the deployed server all
 * need different values.
 *
 *   EXPO_PUBLIC_API_BASE=http://192.168.0.69:8000  npx expo start   (LAN)
 *   EXPO_PUBLIC_API_BASE=https://gist.azurecontainerapps.io         (deployed)
 */
module.exports = {
  expo: {
    name: "Gist",
    slug: "gist",
    version: "1.0.0",
    orientation: "portrait",
    scheme: "gist",
    userInterfaceStyle: "automatic",
    newArchEnabled: true,
    ios: {
      supportsTablet: true,
      bundleIdentifier: "com.distayy.gist",
      infoPlist: {
        // Only needed while pointing at a plain-HTTP dev server on the LAN.
        NSAppTransportSecurity: { NSAllowsLocalNetworking: true },
      },
    },
    plugins: ["expo-router", "expo-secure-store"],
    extra: {
      apiBase: process.env.EXPO_PUBLIC_API_BASE || "http://127.0.0.1:8000",
    },
  },
};
