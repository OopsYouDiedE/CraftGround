package com.kyhsgeekcode.minecraftenv

import com.kyhsgeekcode.minecraftenv.mixin.MouseInputInvoker
import com.kyhsgeekcode.minecraftenv.mixin.MouseXYAccessor
import com.kyhsgeekcode.minecraftenv.proto.ActionSpace
import net.minecraft.client.MinecraftClient
import org.lwjgl.glfw.GLFW.GLFW_KEY_LEFT_SHIFT
import org.lwjgl.glfw.GLFW.GLFW_MOD_SHIFT
import org.lwjgl.glfw.GLFW.GLFW_MOUSE_BUTTON_LEFT
import org.lwjgl.glfw.GLFW.GLFW_MOUSE_BUTTON_RIGHT
import org.lwjgl.glfw.GLFW.GLFW_PRESS
import org.lwjgl.glfw.GLFW.GLFW_RELEASE

object MouseInfo {
    var handle: Long = 0
    var mouseX: Double = 0.0
    var mouseY: Double = 0.0
    var showCursor: Boolean = false
    var currentState: MutableMap<Int, Boolean> = mutableMapOf()

    val buttonMappings =
        mapOf(
            "use" to GLFW_MOUSE_BUTTON_RIGHT,
            "attack" to GLFW_MOUSE_BUTTON_LEFT,
        )

    fun onAction(actionDict: ActionSpace.ActionSpaceMessageV2) {
        val actions =
            mapOf(
                "use" to actionDict.use,
                "attack" to actionDict.attack,
            )
        val shift = KeyboardInfo.isKeyPressed(GLFW_KEY_LEFT_SHIFT)
        val mods = if (shift) GLFW_MOD_SHIFT else 0
        val mouse = MinecraftClient.getInstance().mouse as MouseInputInvoker
        for ((action, glfwButton) in buttonMappings) {
            val previousState = currentState[glfwButton] ?: false
            val currentState = actions[action] ?: false

            if (!previousState && currentState) {
                this.currentState[glfwButton] = true
                mouse.`minecraftEnv$onMouseButton`(handle, glfwButton, GLFW_PRESS, mods)
            } else if (previousState && !currentState) {
                this.currentState[glfwButton] = false
                mouse.`minecraftEnv$onMouseButton`(handle, glfwButton, GLFW_RELEASE, mods)
            }
        }
    }

    fun getMousePos(): Pair<Double, Double> = Pair(mouseX, mouseY)

    fun setCursorPos(
        x: Double,
        y: Double,
    ) {
        mouseX = x
        mouseY = y
        val client = MinecraftClient.getInstance()
        (client?.mouse as? MouseXYAccessor)?.let { mouse ->
            mouse.setX(x)
            mouse.setY(y)
            mouse.setHasResolutionChanged(false)
        }
    }

    fun setCursorShown(show: Boolean) {
        showCursor = show
    }

    fun moveMouseBy(
        dx: Double,
        dy: Double,
    ) {
        if (dx == 0.0 && dy == 0.0) return
        val mouse = MinecraftClient.getInstance().mouse as MouseInputInvoker
        mouse.`minecraftEnv$onCursorPos`(handle, mouseX, mouseY)
        mouseX += dx
        mouseY += dy
        mouse.`minecraftEnv$onCursorPos`(handle, mouseX, mouseY)
    }
}
