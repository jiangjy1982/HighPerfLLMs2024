import tensorflow as tf
import tensorflow_datasets as tfds

import jax
import jax.numpy as jnp
import flax.linen as nn
from flax.training import train_state

import functools

import flax.linen.attention as attention

import numpy as np

import optax

import time

import orbax.checkpoint as ocp

from jax.experimental.pallas.ops.tpu import flash_attention as pallas_attention

import timing_util



BATCH_IN_SEQUENCES = 1
SEQUENCE_LENGTH = 128

VOCAB_DIM = 256
EMBED_DIM = 2048
FF_DIM = 8192

HEAD_DIM = 128

LAYERS = 8

HEAD_DEPTH = 128
NUM_HEADS = 8

LEARNING_RATE = 1e-6

FSDP = 1
TENSOR = 1

LOG_PERIOD = 10
CHECKPOINT_PERIOD = 1000

mesh = jax.sharding.Mesh(np.reshape(  jax.devices()[0], (FSDP,TENSOR)), ["fsdp", "tp"])
desired_embedding_sharding = jax.sharding.NamedSharding(mesh, jax.sharding.PartitionSpec("fsdp", None, "tp"))  # apply this to things that are BATCH, SEQUENCE, EMBED


# def attention_ourselves(_Q, _K, _V):
#     _weights_unnormalized = jax.numpy.einsum("BSHD,BTHD->BHST", _Q, _K)
#     _weights_unnormalized_to_zero_out = jax.numpy.triu( jax.numpy.ones((_K.shape[1],_K.shape[1]), jax.numpy.bfloat16), 1)
#     _weights = jax.nn.softmax(_weights_unnormalized - 1e6 * _weights_unnormalized_to_zero_out)  ### Creating something of size (B,HEADS, SEQUENCE, SEQUENCE)
#     output = jax.numpy.einsum("BHST,BTHD->BSHD", _weights, _V)

#     return output

def attention_with_masking(_Q, _K, _V, seq_pos=SEQUENCE_LENGTH):
    query_segment_id = jnp.ones( (1,_Q.shape[1]), dtype=jnp.int32)
    kv_segment_id = jnp.ones( (1, SEQUENCE_LENGTH), jnp.int32) * jnp.expand_dims(jnp.arange(SEQUENCE_LENGTH) <= seq_pos, axis = 0)

    segment_ids = pallas_attention.SegmentIds( q = query_segment_id, kv = kv_segment_id)
    return jax.numpy.swapaxes(pallas_attention.mha_reference(jax.numpy.swapaxes(_Q,1,2), jax.numpy.swapaxes(_K,1,2), jax.numpy.swapaxes(_V,1,2), None, segment_ids = segment_ids),1,2)

class OurModel(nn.Module):
  @nn.compact
  def __call__(self, x, pos, kv_cache):
    '''
        x is [BATCH, SEQUENCE]
    '''
    embedding = self.param(
        'embedding',
        nn.with_partitioning(nn.initializers.normal(1), ("tp", "fsdp")),
        (VOCAB_DIM, EMBED_DIM),
        jnp.float32,
    )
    x = embedding[x] ##OUTPUT should be [BATCH, SEQUENCE, EMBED]




    for i in range(LAYERS):

      x = nn.LayerNorm( name="layer_norm_" + str(i),)(x)

      positional_embedding = self.param(
        'positional_embedding_' + str(i),
        nn.with_partitioning(nn.initializers.normal(1), (None, None, "fsdp")),
        (1, SEQUENCE_LENGTH, EMBED_DIM),
        jnp.float32,
      )

      x += jax.lax.dynamic_slice_in_dim(positional_embedding, pos, 1, axis=1)

      x = jax.lax.with_sharding_constraint(x, desired_embedding_sharding)
      feedforward = self.param(
          'feedforward_' + str(i),
          nn.with_partitioning(nn.initializers.lecun_normal(), ('fsdp', 'tp')),
          (EMBED_DIM, FF_DIM),
          jnp.float32,
      )
      x = x @ feedforward
      x = jax.nn.relu(x)
      x = jax.lax.with_sharding_constraint(x, desired_embedding_sharding)
      embed = self.param(
          'embed_' + str(i),
          nn.with_partitioning(nn.initializers.lecun_normal(), ('tp', 'fsdp')),
          (FF_DIM, EMBED_DIM),
          jnp.float32,
      )
      x = x @ embed
      x = jax.nn.relu(x)

      q_proj = self.param(
          'qproj_' + str(i),
          nn.with_partitioning(nn.initializers.lecun_normal(), ('fsdp', 'tp')),
          (EMBED_DIM, NUM_HEADS, HEAD_DIM),
          jnp.float32,
      )
      q = jnp.einsum("BSE,EHD->BSHD",x, q_proj )

      k_proj = self.param(
          'kproj_' + str(i),
          nn.with_partitioning(nn.initializers.lecun_normal(), ('fsdp', 'tp')),
          (EMBED_DIM, NUM_HEADS, HEAD_DIM),
          jnp.float32,
      )
      k = jnp.einsum("BSE,EHD->BSHD",x, k_proj )
      ### write to the KV cache
      kv_cache[f"key_{i}"] = jax.lax.dynamic_update_index_in_dim(kv_cache[f"key_{i}"], k, pos, 1)
      k = kv_cache[f"key_{i}"]
      ### use that KV cache entry

      v_proj = self.param(
          'vproj_' + str(i),
          nn.with_partitioning(nn.initializers.lecun_normal(), ('fsdp', 'tp')),
          (EMBED_DIM, NUM_HEADS, HEAD_DIM),
          jnp.float32,
      )
      v = jnp.einsum("BSE,EHD->BSHD",x, v_proj )
      kv_cache[f"value_{i}"] = jax.lax.dynamic_update_index_in_dim(kv_cache[f"value_{i}"], v, pos, 1)
      v = kv_cache[f"value_{i}"]

      o = attention_with_masking(q,k,v, pos)

      o_proj = self.param(
          'oproj_' + str(i),
          nn.with_partitioning(nn.initializers.lecun_normal(), ('fsdp', 'tp')),
          (NUM_HEADS, HEAD_DIM, EMBED_DIM),
          jnp.float32,
      )
      x = jnp.einsum("BSHD,HDE->BSE",o, o_proj )
      x = jax.lax.with_sharding_constraint(x, desired_embedding_sharding)

    return x @ embedding.T, kv_cache


def numpy_to_string(numpy_arr):
    return "".join([chr(item) for item in numpy_arr])

def convert_to_ascii(string_array, max_length):
  result = np.zeros((len(string_array), max_length), dtype=np.uint8)
  for i, string in enumerate(string_array):
    for j, char in enumerate(string):
      if j >= SEQUENCE_LENGTH:
         break
      result[i, j] = ord(char)
  return result

def output_to_input(np_array):
   zero_array = np.zeros( (BATCH_IN_SEQUENCES,SEQUENCE_LENGTH), dtype = jnp.uint8)
   zero_array[:, 1:SEQUENCE_LENGTH] = np_array[:, 0:SEQUENCE_LENGTH-1]
   return zero_array

def calculate_loss(params, model, inputs, outputs):
   proposed_outputs = model.apply(params, inputs)
   one_hot = jax.nn.one_hot(outputs, VOCAB_DIM)
   loss = optax.softmax_cross_entropy(proposed_outputs, one_hot)
   return jnp.mean(loss)


def step(state, model, inputs, outputs):
   loss, grad = jax.value_and_grad(calculate_loss)(state.params, model, inputs, outputs)
   state = state.apply_gradients(grads = grad)
   return loss, state

def calculate_num_params(pyt):
   sizes = jax.tree_util.tree_map(lambda x: x.size, pyt)
   return jax.tree_util.tree_reduce(lambda x, y: x+y, sizes)

def visualize_input_to_output(input_string, output_string):
    for i in range(30):
        print(f"{i}: {input_string[:i]} -> `{output_string[i]}`")


def create_kv_cache():
   output = {}
   for i in range(LAYERS):
      output[f"key_{i}"] = jnp.zeros( (BATCH_IN_SEQUENCES, SEQUENCE_LENGTH, NUM_HEADS, HEAD_DEPTH))
      output[f"value_{i}"] = jnp.zeros( (BATCH_IN_SEQUENCES, SEQUENCE_LENGTH, NUM_HEADS, HEAD_DEPTH))
   return output

def main():
    kv_cache = create_kv_cache()

    rngkey = jax.random.key(0)
    model = OurModel()

    shaped_init = jax.eval_shape( functools.partial(model.init, rngkey), jax.ShapeDtypeStruct((BATCH_IN_SEQUENCES, 1), dtype = jnp.uint8), 0, kv_cache)
    state_sharding = nn.get_sharding(shaped_init, mesh)
    _params = jax.jit(model.init, out_shardings = state_sharding)(rngkey, jax.ShapeDtypeStruct((BATCH_IN_SEQUENCES, 1), dtype = jnp.uint8), 0, kv_cache)
    
    number_total_floats = calculate_num_params(_params) + calculate_num_params(kv_cache)
    number_bytes_to_read = number_total_floats * 4

    print(f"Number bytes {number_bytes_to_read/1e9} GB")

    tx = optax.adam(learning_rate = LEARNING_RATE)
    state = train_state.TrainState.create(
       apply_fn = model.apply,
       params = _params,
       tx = tx
    )

    text = jnp.zeros( (1, 1), dtype = np.int32)
    step_time_ms = timing_util.simple_timeit( jax.jit(model.apply), state.params, text, 0, kv_cache, task="taskname")

    print(f"{step_time_ms=}")
    print(f"memory bandwidth utilization GB/s {(number_bytes_to_read/1e9)/(step_time_ms/1000)}")

    return 0
    abstract_state = jax.tree_util.tree_map(ocp.utils.to_shape_dtype_struct, state)
    checkpointer = ocp.StandardCheckpointer()
    state = checkpointer.restore('/home/rwitten/class_checkpoints/checkpoint_0078000', args=ocp.args.StandardRestore(abstract_state))

    
    NUM_TOKENS = 30
    output_string = ""
    for i in range(NUM_TOKENS):
        logits, kv_cache = model.apply(state.params, text, i, kv_cache) # here is my probability distribution! [BATCH, SEQUENCE, VOCAB]
        text = jax.numpy.argmax(logits, axis=2)
        output_string += chr(text[0,0])

    print(f"Output: `{output_string}`")

if __name__ == "__main__":
    main()