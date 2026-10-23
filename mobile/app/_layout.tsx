import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { useColorScheme } from "react-native";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { useTheme } from "../src/theme";

export default function RootLayout() {
  const t = useTheme();
  const scheme = useColorScheme();
  return (
    <SafeAreaProvider>
      <StatusBar style={scheme === "dark" ? "light" : "dark"} />
      <Stack
        screenOptions={{
          headerStyle: { backgroundColor: t.bgDoc },
          headerTintColor: t.ink,
          headerTitleStyle: { fontWeight: "600" },
          contentStyle: { backgroundColor: t.bgDoc },
        }}
      >
        <Stack.Screen name="index" options={{ title: "Gist" }} />
        <Stack.Screen name="note/[id]" options={{ title: "" }} />
      </Stack>
    </SafeAreaProvider>
  );
}
