package com.kyhsgeekcode.minecraftenv

import net.minecraft.block.Block
import net.minecraft.block.Blocks
import net.minecraft.client.MinecraftClient
import net.minecraft.entity.effect.StatusEffectInstance
import net.minecraft.nbt.NbtCompound
import net.minecraft.server.network.ServerPlayerEntity
import net.minecraft.structure.StructurePlacementData
import net.minecraft.structure.StructureTemplate
import net.minecraft.util.math.BlockPos
import net.minecraft.util.math.Box
import net.minecraft.util.math.Vec3i
import java.util.concurrent.ConcurrentHashMap

object MemorySnapshotStore {
    private data class Snapshot(
        val template: StructureTemplate,
        val origin: BlockPos,
        val playerNbt: NbtCompound,
        val statusEffects: List<StatusEffectInstance>,
    )

    private val snapshots = ConcurrentHashMap<String, Snapshot>()

    fun handle(
        command: String,
        client: MinecraftClient,
    ): Boolean {
        if (!command.startsWith("memorysnapshot ")) return false
        val arguments = command.split(" ")
        require(arguments.size >= 3) { "memorysnapshot command requires more arguments" }
        val operation = arguments[1]
        val snapshotId = arguments[2]
        val server = requireNotNull(client.server) { "Server is not ready" }
        when (operation) {
            "save" -> {
                require(arguments.size == 9) {
                    "Usage: memorysnapshot save <id> <x1> <y1> <z1> <x2> <y2> <z2>"
                }
                val first =
                    BlockPos(
                        arguments[3].toInt(),
                        arguments[4].toInt(),
                        arguments[5].toInt(),
                    )
                val second =
                    BlockPos(
                        arguments[6].toInt(),
                        arguments[7].toInt(),
                        arguments[8].toInt(),
                    )
                val minimum =
                    BlockPos(
                        minOf(first.x, second.x),
                        minOf(first.y, second.y),
                        minOf(first.z, second.z),
                    )
                val maximum =
                    BlockPos(
                        maxOf(first.x, second.x),
                        maxOf(first.y, second.y),
                        maxOf(first.z, second.z),
                    )
                server.execute {
                    val player = singlePlayer(server.playerManager.playerList)
                    val template = StructureTemplate()
                    template.saveFromWorld(
                        player.serverWorld,
                        minimum,
                        Vec3i(
                            maximum.x - minimum.x + 1,
                            maximum.y - minimum.y + 1,
                            maximum.z - minimum.z + 1,
                        ),
                        true,
                        Blocks.STRUCTURE_VOID,
                    )
                    snapshots[snapshotId] =
                        Snapshot(
                            template,
                            minimum,
                            player.writeNbt(NbtCompound()),
                            player.statusEffects.map(::StatusEffectInstance),
                        )
                }
            }

            "load" -> {
                require(arguments.size == 3) { "Usage: memorysnapshot load <id>" }
                client.setScreen(null)
                server.execute {
                    val snapshot =
                        requireNotNull(snapshots[snapshotId]) {
                            "Memory snapshot does not exist: $snapshotId"
                        }
                    val player = singlePlayer(server.playerManager.playerList)
                    val world = player.serverWorld
                    world
                        .getOtherEntities(
                            player,
                            Box(
                                snapshot.origin.toCenterPos(),
                                snapshot.origin.add(snapshot.template.size).toCenterPos(),
                            ),
                        ).forEach { it.discard() }
                    snapshot.template.place(
                        world,
                        snapshot.origin,
                        snapshot.origin,
                        StructurePlacementData().setIgnoreEntities(false).setUpdateNeighbors(true),
                        world.random,
                        Block.NOTIFY_ALL,
                    )
                    player.readNbt(snapshot.playerNbt.copy())
                    player.clearStatusEffects()
                    snapshot.statusEffects.forEach {
                        player.addStatusEffect(StatusEffectInstance(it))
                    }
                    player.closeHandledScreen()
                    player.inventory.markDirty()
                    player.currentScreenHandler.sendContentUpdates()
                    player.networkHandler.requestTeleport(
                        player.x,
                        player.y,
                        player.z,
                        player.yaw,
                        player.pitch,
                    )
                    player.velocityDirty = true
                }
            }

            else -> error("Unsupported memorysnapshot operation: $operation")
        }
        return true
    }

    private fun singlePlayer(players: List<ServerPlayerEntity>): ServerPlayerEntity {
        require(players.size == 1) {
            "memorysnapshot requires a single-player environment, found ${players.size} players"
        }
        return players.single()
    }
}
