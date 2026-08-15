package com.kyhsgeekcode.minecraftenv.mixin;

import com.kyhsgeekcode.minecraftenv.ControlMode;
import net.minecraft.client.MinecraftClient;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

@Mixin(MinecraftClient.class)
public class ClientIsWindowFocusedMixin {
  @Inject(method = "isWindowFocused", at = @At("HEAD"), cancellable = true)
  private void forceFocusedForAgent(CallbackInfoReturnable<Boolean> cir) {
    if (!ControlMode.isHuman()) {
      cir.setReturnValue(true);
    }
  }
}
