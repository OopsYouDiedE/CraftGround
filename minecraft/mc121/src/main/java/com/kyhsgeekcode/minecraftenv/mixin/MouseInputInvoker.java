package com.kyhsgeekcode.minecraftenv.mixin;

import net.minecraft.client.Mouse;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.gen.Invoker;

@Mixin(Mouse.class)
public interface MouseInputInvoker {
  @Invoker("onCursorPos")
  void minecraftEnv$onCursorPos(long window, double x, double y);

  @Invoker("onMouseButton")
  void minecraftEnv$onMouseButton(long window, int button, int action, int modifiers);
}
