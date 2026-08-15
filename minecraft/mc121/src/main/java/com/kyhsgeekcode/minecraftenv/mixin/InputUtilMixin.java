package com.kyhsgeekcode.minecraftenv.mixin;

import com.kyhsgeekcode.minecraftenv.KeyboardInfo;
import com.kyhsgeekcode.minecraftenv.MouseInfo;
import com.kyhsgeekcode.minecraftenv.ControlMode;
import org.lwjgl.glfw.*;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Overwrite;

@Mixin(net.minecraft.client.util.InputUtil.class)
public class InputUtilMixin {
  @Overwrite
  public static boolean isKeyPressed(long handle, int code) {
    if (ControlMode.isHuman()) {
      return GLFW.glfwGetKey(handle, code) == GLFW.GLFW_PRESS;
    }
    return KeyboardInfo.INSTANCE.isKeyPressed(code);
  }

  @Overwrite
  public static void setMouseCallbacks(
      long handle,
      GLFWCursorPosCallbackI cursorPosCallback,
      GLFWMouseButtonCallbackI mouseButtonCallback,
      GLFWScrollCallbackI scrollCallback,
      GLFWDropCallbackI dropCallback) {
    if (ControlMode.isHuman()) {
      GLFW.glfwSetCursorPosCallback(handle, cursorPosCallback);
      GLFW.glfwSetMouseButtonCallback(handle, mouseButtonCallback);
      GLFW.glfwSetScrollCallback(handle, scrollCallback);
      GLFW.glfwSetDropCallback(handle, dropCallback);
      return;
    }
    MouseInfo.INSTANCE.setHandle(handle);
  }

  @Overwrite
  public static void setCursorParameters(long handler, int inputModeValue, double x, double y) {
    if (ControlMode.isHuman()) {
      GLFW.glfwSetInputMode(handler, GLFW.GLFW_CURSOR, inputModeValue);
      GLFW.glfwSetCursorPos(handler, x, y);
      return;
    }
    MouseInfo.INSTANCE.setCursorPos(x, y);
    MouseInfo.INSTANCE.setCursorShown(inputModeValue == GLFW.GLFW_CURSOR_NORMAL);
  }

  @Overwrite
  public static void setKeyboardCallbacks(
      long handle, GLFWKeyCallbackI keyCallback, GLFWCharModsCallbackI charModsCallback) {
    if (ControlMode.isHuman()) {
      GLFW.glfwSetKeyCallback(handle, keyCallback);
      GLFW.glfwSetCharModsCallback(handle, charModsCallback);
      return;
    }
    KeyboardInfo.INSTANCE.setHandle(handle);
  }
}
