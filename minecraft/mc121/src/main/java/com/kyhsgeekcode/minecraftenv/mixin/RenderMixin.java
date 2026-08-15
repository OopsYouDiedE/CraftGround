package com.kyhsgeekcode.minecraftenv.mixin;

import com.kyhsgeekcode.minecraftenv.MinecraftEnv;
import com.kyhsgeekcode.minecraftenv.ControlMode;
import com.mojang.blaze3d.systems.RenderSystem;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.gl.Framebuffer;
import net.minecraft.client.render.Tessellator;
import net.minecraft.client.util.Window;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Mixin(MinecraftClient.class)
public class RenderMixin {
  @Inject(method = "tick", at = @At("HEAD"), cancellable = true)
  private void consumeActionBeforeClientTick(CallbackInfo callbackInfo) {
    if (!MinecraftEnv.beforeClientTick()) {
      callbackInfo.cancel();
    }
  }

  @Inject(method = "render", at = @At("RETURN"))
  private void sendObservationAfterRender(boolean tick, CallbackInfo callbackInfo) {
    MinecraftEnv.onRenderComplete();
  }

  @Redirect(
      method = "render",
      at = @At(value = "INVOKE", target = "Lnet/minecraft/client/gl/Framebuffer;endWrite()V"))
  private void frameBufferEndWrite(Framebuffer instance) {
    if (ControlMode.isHuman()) {
      instance.endWrite();
    }
  }

  @Redirect(
      method = "render",
      at = @At(value = "INVOKE", target = "Lnet/minecraft/client/gl/Framebuffer;draw(II)V"))
  private void frameBufferDraw(Framebuffer instance, int width, int height) {
    if (ControlMode.isHuman()) {
      instance.draw(width, height);
    }
  }

  @Redirect(
      method = "render",
      at = @At(value = "INVOKE", target = "Lnet/minecraft/client/util/Window;swapBuffers()V"))
  private void windowSwapBuffers(Window instance) {
    if (ControlMode.isHuman()) {
      instance.swapBuffers();
      return;
    }
    RenderSystemPollEventsInvoker.pollEvents();
    RenderSystem.replayQueue();
    Tessellator.getInstance().clear();
    //        GLFW.glfwSwapBuffers(window);
    RenderSystemPollEventsInvoker.pollEvents();
  }
}
