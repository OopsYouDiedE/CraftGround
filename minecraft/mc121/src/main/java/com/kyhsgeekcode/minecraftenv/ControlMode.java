package com.kyhsgeekcode.minecraftenv;

/** Selects whether CraftGround or the native Minecraft window owns player input. */
public final class ControlMode {
  private static final String HUMAN = "human";

  private ControlMode() {}

  public static boolean isHuman() {
    return HUMAN.equalsIgnoreCase(System.getenv("CRAFTGROUND_CONTROL_MODE"));
  }
}
