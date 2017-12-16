import numpy as np
import tensorflow as tf
import random
import time
from copy import deepcopy

from draftstate import DraftState
import matchProcessing as mp
import experienceReplay as er
from rewards import getReward
from dueling_networks import self_train

def trainNetwork(online_net, target_net, training_matches, validation_matches, train_epochs, batch_size, buffer_size, dampen_states = False, load_model = False, verbose = False):
    """
    Args:
        online_net (qNetwork): "live" Q-network to be trained.
        target_net (qNetwork): target Q-network used to generate target values for the online network
        training_matches (list(match)): list of matches to be trained on
        validation_matches (list(match)): list of matches to validate model against
        train_epochs (int): number of times to learn on given data
        batch_size (int): size of each training set sampled from the replay buffer which will be used to update Qnet at a time
        buffer_size (int): size of replay buffer used
        dampen_states (bool): flag for running dampening routine on model
        load_model (bool): flag to reload existing model
        verbose (bool): flag for enhanced output
    Returns:
        (loss,validation_accuracy) tuple
    Trains the Q-network Qnet in batches using experience replays.
    """
    num_episodes = len(training_matches)
    if(verbose):
        print("***")
        print("Beginning training..")
        print("  train_epochs: {}".format(train_epochs))
        print("  num_episodes: {}".format(num_episodes))
        print("  batch_size: {}".format(batch_size))
        print("  buffer_size: {}".format(buffer_size))
        if(dampen_states):
            print("  ********************************")
            print("  WARNING: BEGINNING DAMPENING CYCLES")
            print("  THIS SHOULD ONLY BE USED TO REDUCE VALUATION FOR OLDER METAS")
            print("  ********************************")
            time.sleep(2.)
    # Hyperparameter used in updating target network
    # Some notable values:
    #  tau = 1.e-3 -> used in original paper
    #  tau = 0.5 -> average DDQN
    #  tau = 1.0 -> copy online -> target
    tau = 1.
    target_update_frequency = 10000 # How often to update target network. Should only be used with tau = 1.
    stash_model = True # Flag for stashing a copy of the model
    model_stash_interval = 10 # Stashes a copy of the model this often
    # Number of steps to take before training. Allows buffer to partially fill.
    # Must be at least batch_size to avoid error when sampling from experience replay
    pre_training_steps = 10*batch_size
    assert(pre_training_steps <= buffer_size), "Replay not large enough for pre-training!"
    assert(pre_training_steps >= batch_size), "Buffer not allowed to fill enough before sampling!"
    # Number of steps to force learner to observe submitted actions, rather than submit its own actions
    observations = 2000
    epsilon = 0.5 # Initial probability of letting the learner submit its own action
    eps_decay_rate = 1./(25*20*len(training_matches)) # Rate at which epsilon decays per submission
    # Number of steps to take between training
    update_freq = 1 # There are 10 submissions per match per side
    overwrite_initial_lr = 2.0e-5 # Overwrite default lr for network
    lr_decay_freq = 5 # Decay learning rate after a set number of epochs
    min_learning_rate = 1.e-8 # Minimum learning rate allowed to decay to

    teams = [DraftState.BLUE_TEAM, DraftState.RED_TEAM]
    # We can't validate a winner for submissions generated by the learner,
    # so we will use a winner-less match when getting rewards for such states
    blank_match = {"winner":None}
    loss_over_epochs = []
    total_steps = 0
    # Start training
    with tf.Session() as sess:
        sess.run(tf.global_variables_initializer())
        if load_model:
            # Open saved model
            path_to_model = "tmp/model_E{}.ckpt".format(25)
            #path_to_model = "model_predictions/play_ins_rd2/model_play_ins_rd2.ckpt"
            online_net.saver.restore(sess,path_to_model)
            print("\nCheckpoint loaded from {}".format(path_to_model))

            if(overwrite_initial_lr):
                online_net.learning_rate.assign(overwrite_initial_lr).eval()

        # Add target init and update operations to graph
        target_init = create_target_initialization_ops(target_net.name, online_net.name)
        target_update = create_target_update_ops(target_net.name,online_net.name,tau)
        # Initialize target network
        sess.run(target_init)

        # Get initial loss and accuracy estimates
        val_loss,val_acc = validate_model(sess, validation_matches, online_net, target_net)
        loss,train_acc = validate_model(sess, training_matches, online_net, target_net)
        print(" Initial loss {:.6f}, train {:.6f}, val {:.6f}".format(loss,train_acc,val_acc),flush=True)

        # Initialize experience replay buffer
        experience_replay = er.ExperienceBuffer(buffer_size)
        for i in range(train_epochs):
            t0 = time.time()
            if((i>0) and (i % lr_decay_freq == 0) and (online_net.learning_rate.eval() >= min_learning_rate)):
                # Decay learning rate accoring to decay schedule
                online_net.learning_rate = 0.50*online_net.learning_rate

            epoch_steps = 0

            bad_state_counts = {
                "wins":{DraftState.BAN_AND_SUBMISSION:0,
                        DraftState.DUPLICATE_SUBMISSION:0,
                        DraftState.DUPLICATE_ROLE:0,
                        DraftState.INVALID_SUBMISSION:0,
                        DraftState.TOO_MANY_BANS:0,
                        DraftState.TOO_MANY_PICKS:0},
                "loss":{DraftState.BAN_AND_SUBMISSION:0,
                        DraftState.DUPLICATE_SUBMISSION:0,
                        DraftState.DUPLICATE_ROLE:0,
                        DraftState.INVALID_SUBMISSION:0,
                        DraftState.TOO_MANY_BANS:0,
                        DraftState.TOO_MANY_PICKS:0}}
            learner_submitted_counts = 0
            null_action_count = 0

            # Shuffle match presentation order
            shuffled_matches = random.sample(training_matches,len(training_matches))

            # Run model through a self-training iteration, including exploration
            experiences = self_train(sess, epsilon, n_experiences=20)
            # If self training results in illegal states, add it to memory
            if experiences:
                print("adding {} self-trained experiences..".format(len(experiences)))
#                for exp in experiences:
#                    _,_,r,_ = exp
#                    print("reward (should be negative) = {}".format(r))
                experience_replay.store(experiences)
                learner_submitted_counts += len(experiences)

            for match in shuffled_matches:
                for team in teams:
                    # Process match into individual experiences
                    experiences = mp.processMatch(match, team)
                    for experience in experiences:
                        # Some experiences include NULL submissions
                        # The learner isn't allowed to submit NULL picks so skip adding these
                        # to the buffer.
                        state,actual,_,_ = experience
                        (cid,pos) = actual
                        if cid is None:
                            null_action_count += 1
                            continue
                        # Store original experience
                        experience_replay.store([experience])
                        if(total_steps >= observations):
                            # Let the network predict the next action, if the action leads
                            # to an invalid state add a negatively reinforced experience to the replay buffer.
                            random_submission = False
                            if(random.random() < epsilon):
                                random_submission = True
                                # Explore state space by submitting random action and checking if that action is legal
                                pred_act = [random.randint(0,state.num_actions-1)]
                            else:
                                # Let model make prediction
                                pred_Q = sess.run(online_net.outQ,
                                                feed_dict={online_net.input:[state.format_state()],
                                                           online_net.secondary_input:[state.format_secondary_inputs()]})
                                sorted_actions = pred_Q[0,:].argsort()[::-1]
                                pred_act = sorted_actions[0:4] # top 5 actions by model

                            top_action = pred_act[0]
                            for action in pred_act:
                                (cid,pos) = state.formatAction(action)

                                pred_state = deepcopy(state)
                                pred_state.updateState(cid,pos)

                                state_code = pred_state.evaluateState()
                                r = getReward(pred_state, blank_match, (cid,pos), actual)
                                new_experience = (state, (cid,pos), r, pred_state)
                                if(state_code in DraftState.invalid_states):
                                    # Prediction moves to illegal state, add negative experience
                                    if(team==match["winner"]):
                                        bad_state_counts["wins"][state_code] += 1
                                    else:
                                        bad_state_counts["loss"][state_code] += 1
                                    experience_replay.store([new_experience])
                                elif(not random_submission and (cid,pos) != actual and action == top_action):
                                    # Add memories for "best" legal submission if it was chosen by model and does not duplicate already submitted memory
                                    learner_submitted_counts += 1
                                    experience_replay.store([new_experience])

                        if(epsilon > 0.1):
                            # Reduce epsilon over time
                            epsilon -= eps_decay_rate
                        total_steps += 1
                        epoch_steps += 1

                        # Every update_freq steps we train the network using samples from the replay buffer
                        if((total_steps >= pre_training_steps) and (total_steps % update_freq == 0)):
                            training_batch = experience_replay.sample(batch_size)

                            # Calculate target Q values for each example:
                            # For non-terminal states, targetQ is estimated according to
                            #   targetQ = r + gamma*Q'(s',max_a Q(s',a))
                            # where Q' denotes the target network.
                            # For terminating states the target is computed as
                            #   targetQ = r
                            updates = []
                            for exp in training_batch:
                                startState,_,reward,endingState = exp
                                if(dampen_states):
                                    # To dampen states (usually done after major patches or when the meta shifts)
                                    # we replace winning rewards with 0. (essentially a loss).
                                    reward = 0.
                                state_code = endingState.evaluateState()
                                if(state_code==DraftState.DRAFT_COMPLETE or state_code in DraftState.invalid_states):
                                    # Action moves to terminal state
                                    updates.append(reward)
                                else:
                                    # Follwing double DQN paper (https://arxiv.org/abs/1509.06461).
                                    #  Action is chosen by online network, but the target network is used to evaluate this policy.
                                    # Each row in predicted_Q gives estimated Q(s',a) values for all possible actions for the input state s'.
                                    predicted_action = sess.run(online_net.prediction,
                                                        feed_dict={online_net.input:[endingState.format_state()],
                                                                   online_net.secondary_input:[endingState.format_secondary_inputs()]})[0]
                                    predicted_Q = sess.run(target_net.outQ,
                                                        feed_dict={target_net.input:[endingState.format_state()],
                                                                   target_net.secondary_input:[endingState.format_secondary_inputs()]})
                                    updates.append(reward + online_net.discount_factor*predicted_Q[0,predicted_action])

                            targetQ = np.array(updates)
                            targetQ.shape = (batch_size,)

                            # Update online net using target Q
                            # Experience replay stores action = (champion_id, position) pairs
                            # these need to be converted into the corresponding index of the input vector to the Qnet
                            actions = np.array([startState.getAction(*exp[1]) for exp in training_batch])
                            _ = sess.run(online_net.update,
                                     feed_dict={online_net.input:np.stack([exp[0].format_state() for exp in training_batch],axis=0),
                                                online_net.secondary_input:np.stack([exp[0].format_secondary_inputs() for exp in training_batch],axis=0),
                                                online_net.actions:actions,
                                                online_net.target:targetQ,
                                                online_net.dropout_keep_prob:0.5})
                            if(total_steps % target_update_frequency == 0):
                                # After the online network has been updated, update target network
                                _ = sess.run(target_update)

            t1 = time.time()-t0
            val_loss,val_acc = validate_model(sess, validation_matches, online_net, target_net)
            loss,train_acc = validate_model(sess, training_matches, online_net, target_net)
            loss_over_epochs.append(loss)
            # Once training is complete, save the updated network
            out_path = online_net.saver.save(sess,"tmp/model_E{}.ckpt".format(train_epochs))
            if(verbose):
                print(" Finished epoch {}/{}: dt {:.2f}, mem {}, loss {:.6f}, train {:.6f}, val {:.6f}".format(i+1,train_epochs,t1,epoch_steps,loss,train_acc,val_acc),flush=True)
                print("  alpha:{:.4e}".format(online_net.learning_rate.eval()))
                invalid_action_count = sum([bad_state_counts["wins"][k]+bad_state_counts["loss"][k] for k in bad_state_counts["wins"]])
                print("  negative memories added = {}".format(invalid_action_count))
                print("  bad state distributions:")
                print("   from wins: {:9} from losses:".format(""))
                for code in bad_state_counts["wins"]:
                    print("   {:3} -> {:3} counts {:2} {:3} -> {:3} counts".format(code,bad_state_counts["wins"][code],"",code,bad_state_counts["loss"][code]))
                print("  learner submissions: {}".format(learner_submitted_counts))
                print("  model is saved in file: {}".format(out_path))
                print("***",flush=True)
            if(stash_model):
                if(i>0 and (i+1)%model_stash_interval==0):
                    # Stash a copy of the current model
                    out_path = online_net.saver.save(sess,"tmp/models/model_E{}.ckpt".format(i+1))
                    print("Stashed a copy of the current model in {}".format(out_path))


    stats = (loss_over_epochs,train_acc)
    return stats

def create_target_update_ops(target_scope, online_scope, tau=1e-3, name="target_update"):
    """
    Adds operations to graph which are used to update the target network after after a training batch is sent
    through the online network.

    This function should be executed only once before training begins. The resulting operations should
    be run within a tf.Session() once per training batch.

    In double-Q network learning, the online (primary) network is updated using traditional backpropegation techniques
    with target values produced by the target-Q network.
    To improve stability, the target-Q is updated using a linear combination of its current weights
    with the current weights of the online network:
        Q_target = tau*Q_online + (1-tau)*Q_target
    Typical tau values are small (tau ~ 1e-3). For more, see https://arxiv.org/abs/1509.06461 and https://arxiv.org/pdf/1509.02971.pdf.
    Args:
        target_scope (str): name of scope that target network occupies
        online_scope (str): name of scope that online network occupies
        tau (float32): Hyperparameter for combining target-Q and online-Q networks
        name (str): name of operation which updates the target network when run within a session
    Returns: Tensorflow operation which updates the target nework when run.
    """
    target_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=target_scope)
    online_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=online_scope)
    ops = [target_params[i].assign(tf.add(tf.multiply(tau,online_params[i]),tf.multiply(1.-tau,target_params[i]))) for i in range(len(target_params))]
    return tf.group(*ops,name=name)

def create_target_initialization_ops(target_scope, online_scope):
    """
    This adds operations to the graph in order to initialize the target Q network to the same values as the
    online network.

    This function should be executed only once just after the online network has been initialized.

    Args:
        target_scope (str): name of scope that target network occupies
        online_scope (str): name of scope that online network occupies
    Returns:
        Tensorflow operation (named "target_init") which initialize the target nework when run.
    """
    return create_target_update_ops(target_scope, online_scope, tau=1.0, name="target_init")

def validate_model(sess, validation_data, online_net, target_net):
    """
    Validates given model by computing loss and absolute accuracy for validation data using current Qnet estimates.
    Args:
        sess (tensorflow Session): TF Session to run model in
        validation_data (list(dict)): list of matches to validate against
        online_net (qNetwork): "live" Q-network to be validated
        target_net (qNetwork): target Q-network used to generate target values
    Returns:
        stats (tuple(float)): list of statistical measures of performance. stats = (loss,acc)
    """
    val_replay = er.ExperienceBuffer(10*len(validation_data))
    for match in validation_data:
        # Loss is only computed for winning side of drafts
        team = DraftState.RED_TEAM if match["winner"]==1 else DraftState.BLUE_TEAM
        # Process match into individual experiences
        experiences = mp.processMatch(match, team)
        for exp in experiences:
            _,act,_,_ = exp
            (cid,pos) = act
            if cid is None:
                # Skip null actions such as missing/skipped bans
                continue
            val_replay.store([exp])

    n_experiences = val_replay.getBufferSize()
    val_experiences = val_replay.sample(n_experiences)
    state,_,_,_ = val_experiences[0]
    val_states = np.zeros((n_experiences,)+state.format_state().shape)
    val_secondary_inputs = np.zeros((n_experiences,)+state.format_secondary_inputs().shape)
    val_actions = np.zeros((n_experiences,))
    val_targets = np.zeros((n_experiences,))
    for n in range(n_experiences):
        start,act,rew,finish = val_experiences[n]
        val_states[n,:,:] = start.format_state()
        val_secondary_inputs[n,:] = start.format_secondary_inputs()
        (cid,pos) = act
        val_actions[n] = start.getAction(cid,pos)
        state_code = finish.evaluateState()
        if(state_code==DraftState.DRAFT_COMPLETE or state_code in DraftState.invalid_states):
            # Action moves to terminal state
            val_targets[n] = rew
        else:
            # Each row in predictedQ gives estimated Q(s',a) values for each possible action for the input state s'.
            predicted_Q = sess.run(target_net.outQ,
                            feed_dict={target_net.input:[finish.format_state()],
                            target_net.secondary_input:[finish.format_secondary_inputs()]})
            # To get max_{a} Q(s',a) values take max along *rows* of predictedQ.
            max_Q = np.max(predicted_Q,axis=1)[0]
            val_targets[n] = (rew + online_net.discount_factor*max_Q)

    loss,pred_actions = sess.run([online_net.loss, online_net.prediction],
                          feed_dict={online_net.input:val_states,
                                online_net.secondary_input:val_secondary_inputs,
                                online_net.actions:val_actions,
                                online_net.target:val_targets})
    accurate_predictions = 0.
    for match in validation_data:
        actions = []
        states = []
        blue_score = score_match(sess,online_net,match,DraftState.BLUE_TEAM)
        red_score = score_match(sess,online_net,match,DraftState.RED_TEAM)
        predicted_winner = DraftState.BLUE_TEAM if blue_score >= red_score else DraftState.RED_TEAM
        match_winner = DraftState.RED_TEAM if match["winner"]==1 else DraftState.BLUE_TEAM
        if predicted_winner == match_winner:
            accurate_predictions +=1
    val_accuracy = accurate_predictions/len(validation_data)
    return (loss, val_accuracy)

def score_match(sess, Qnet, match, team):
    """
    Generates an estimated performance score for a team using a specified Qnetwork.
    Args:
        sess (tensorflow Session): TF Session to run model in
        Qnet (qNetwork): tensorflow q network used to score draft
        match (dict): match dictionary with pick and ban data
        team (DraftState.BLUE_TEAM or DraftState.RED_TEAM): team perspective that is being scored
    Returns:
        score (float): estimated value of picks made in the draft submitted by team for this match
    """
    score = 0.
    actions = []
    states = []
    secondary_inputs = []
    experiences = mp.processMatch(match,team)
    for exp in experiences:
        start,(cid,pos),_,_ = exp
        if cid is None:
            # Ignore missing bans (if present)
            continue
        actions.append(start.getAction(cid,pos))
        states.append(start.format_state())
        secondary_inputs.append(start.format_secondary_inputs())

    # Feed states forward and get scores for submitted actions
    predicted_Q = sess.run(Qnet.outQ,
                    feed_dict={Qnet.input:np.stack(states,axis=0),
                               Qnet.secondary_input:np.stack(secondary_inputs,axis=0)})
    assert len(actions) == predicted_Q.shape[0], "Number of actions doesn't match number of Q estimates!"
    for i in range(len(actions)):
        score += predicted_Q[i,actions[i]]
    return score
